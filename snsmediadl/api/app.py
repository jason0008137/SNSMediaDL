"""FastAPI app 組裝。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from ..config import Config, ensure_output_root, load_config
from ..db import recovery
from ..db.session import make_engine
from ..db.models import Base
from ..services.fetch_queue import FetchQueue

_maker: sessionmaker[Session] | None = None
_config: Config | None = None
_transport: httpx.AsyncBaseTransport | None = None
_fetch_queue: "FetchQueue | None" = None


def get_session() -> Iterator[Session]:
    """Session 依賴。測試用 dependency_overrides 換掉。"""
    assert _maker is not None, "app 尚未初始化"
    with _maker() as session:
        yield session


def get_config() -> Config:
    assert _config is not None, "app 尚未初始化"
    return _config


def get_maker() -> sessionmaker[Session]:
    """給背景工作用的 session 工廠。

    ⚠️ 不可以拿 `get_session` 的那個 Session 去跑下載：它綁在單一 request 的
    生命週期上，而下載會在 response 送出後才結束。
    """
    assert _maker is not None, "app 尚未初始化"
    return _maker


def get_transport() -> httpx.AsyncBaseTransport | None:
    """測試用來塞 MockTransport。正式執行一律是 None（走真實網路）。"""
    return _transport


def get_fetch_queue() -> "FetchQueue":
    """抓取佇列（序列，一次一個帳號）。"""
    assert _fetch_queue is not None, "app 尚未初始化"
    return _fetch_queue


log = logging.getLogger("snsmediadl")


async def _download_loop(
    cfg: Config,
    maker: sessionmaker[Session],
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """背景把佇列清空。

    extension 送進來只是排隊 —— 沒有這個迴圈（或使用者按下 `POST /api/queue/run`），
    檔案永遠不會落地。

    迴圈每輪都重讀 `cfg.auto_download`，所以 GUI 的開關即時生效、不需重啟。

    走 `runner` 而不是直接呼叫 `run_worker`：使用者可能正在手動觸發一輪，
    兩邊併行會撿到同一批 pending（見 `downloader/runner.py`）。
    """
    from ..downloader import runner

    while True:
        if not cfg.auto_download:
            await asyncio.sleep(cfg.poll_interval_seconds)
            continue
        try:
            stats = await runner.run_once(cfg, maker, transport=transport)
            # None = 手動觸發的那輪正在跑。它自己會記 log，這裡不重複記。
            if stats is None:
                pass
            # skipped 也要記：使用者按了重試但檔案還在（hash 相符）時走的是這條路，
            # 不記的話畫面毫無反應，正是這個日誌要消除的疑惑。
            else:
                if stats.done or stats.failed or stats.skipped:
                    log.info(
                        "處理佇列：%s 下載完成 / %s 已存在略過 / %s 失敗",
                        stats.done, stats.skipped, stats.failed,
                    )
                # errors 要在 counters 之外記：被限速時三個計數都是 0，
                # 掛在 if 裡面會把「已被平台限速」這種最該看到的訊息吞掉。
                for err in stats.errors:
                    log.error("%s", err)
        except Exception:  # noqa: BLE001 - 背景迴圈不能因單次例外就停掉
            log.exception("下載迴圈發生例外，稍後重試")
        await asyncio.sleep(cfg.poll_interval_seconds)


def create_app(
    cfg: Config | None = None,
    *,
    create_tables: bool = True,
    enable_worker: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    global _maker, _config, _transport, _fetch_queue

    _config = cfg or load_config()
    _transport = transport
    engine = make_engine(_config)
    if create_tables:
        Base.metadata.create_all(engine)
    _maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    # 單例跨 app 實例共用（測試會建很多個），不清就會帶著上一個的狀態
    from ..downloader import runner as _runner

    _runner.reset()

    async def _download_after_batch() -> None:
        """批次抓完就下載。**明確觸發，不動 auto_download。**

        那個開關管的是每 5 秒自撿的背景迴圈；預設開啟的危險是 429 時媒體
        被標回 pending，迴圈又撿起來再打，等於默默推翻「429 停止不重試」。
        """
        assert _config is not None and _maker is not None
        stats = await _runner.run_once(_config, _maker, transport=_transport)
        if stats is not None:
            log.info(
                "批次抓取後下載：%s 完成 / %s 已存在 / %s 失敗",
                stats.done, stats.skipped, stats.failed,
            )

    _fetch_queue = FetchQueue(
        cfg=_config, maker=_maker, transport=_transport,
        on_drain=_download_after_batch,
    )

    # 迴圈本身一律啟動；要不要真的下載由 cfg.auto_download 逐輪決定，
    # 這樣 GUI 的開關才能即時生效而不用重啟。
    run_worker_task = enable_worker

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = None
        # 一定要留下啟動紀錄：GUI 的日誌區塊空白時，使用者無從分辨
        # 「一切正常沒事發生」和「日誌功能壞了」。
        if _config is not None:
            # 路徑打錯（磁碟沒插、沒寫入權限）要在啟動就炸，不要等到第一次
            # 下載才發現。這裡會丟例外中斷啟動 —— 那是刻意的。
            _config.output_root = ensure_output_root(_config.output_root)
        log.info("SNSMediaDL 啟動，輸出目錄 %s", _config.output_root if _config else "?")
        # crash 收拾：把卡在 downloading 的媒體打回 pending，並檢查完整性。
        # 放在下載迴圈啟動**之前** —— 反過來的話 worker 可能先撿走一批，
        # 回收就會把正在下載的那幾筆一起搶走。
        assert _maker is not None
        recovery.on_startup(engine, _maker)
        if _config is not None and _config.extra_media_roots:
            log.info(
                "額外可讀取的媒體目錄：%s",
                "、".join(str(p) for p in _config.extra_media_roots),
            )
        if _fetch_queue is not None:
            _fetch_queue.start()
        if run_worker_task:
            assert _config is not None and _maker is not None
            log.info(
                "背景下載迴圈已啟動（目前狀態：%s，每 %s 秒檢查）",
                "開啟" if _config.auto_download else "關閉",
                _config.poll_interval_seconds,
            )
            task = asyncio.create_task(_download_loop(_config, _maker, _transport))
        else:
            log.info("背景下載迴圈未啟動（需要時請執行 cli download）")
        try:
            yield
        finally:
            log.info("SNSMediaDL 停止")
            # WAL 併回主檔並截斷。不做的話 WAL 會一路長大，而且沒有徵兆。
            recovery.checkpoint_wal(engine)
            if _fetch_queue is not None:
                await _fetch_queue.stop()
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(
        lifespan=lifespan,
        title="SNSMediaDL",
        version="0.1.0",
        description="本機單機的 SNS 媒體下載後端。不對外開放，不做認證。",
    )

    # 只放行 Chrome extension 與 loopback。
    # 這個 API 沒有認證，開 allow_origins=["*"] 等於讓任何網頁都能讀寫你的下載歷史。
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=(
            r"^(chrome-extension://[a-z]+"
            r"|https?://(127\.0\.0\.1|localhost)(:\d+)?)$"
        ),
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["content-type"],
    )

    from . import (
        creators, deletion, devtools, errors, fetch, files, ingest, logbuf, prefs,
        query, tagging, ugoira,
    )

    logbuf.install()
    # 錯誤回應一律 `{"code", "detail"}`。掛在 HTTPException 上（不是只掛
    # ApiError）—— 形狀不一致的話前端每一個顯示點都要各自防一次。
    errors.install(app)

    app.include_router(fetch.router)
    app.include_router(ingest.router)
    app.include_router(query.router)
    app.include_router(creators.router)
    app.include_router(tagging.router)
    app.include_router(prefs.router)
    app.include_router(files.router)
    app.include_router(ugoira.router)
    app.include_router(devtools.router)
    app.include_router(deletion.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # 靜態頁面必須最後掛：mount 在 "/" 會吃掉所有未匹配的路徑，
    # 先掛的話 API 路由就進不來了。
    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.is_dir():
        app.mount("/", _NoHeuristicCache(directory=web_dir, html=True), name="web")

    return app


# ⚠️ Python 3.12 之前的 `mimetypes` 不認得 `.mjs`，會回 `text/plain`，
# 而瀏覽器**拒絕**把 `text/plain` 當 ES module 匯入。症狀是網頁裡
# 匯入 `.mjs` 的地方全部「載入失敗 Failed to fetch dynamically
# imported module」，而檔案其實回 200 —— 看起來像檔案不見了。
# 3.14 的機器上正常、3.10 的機器上壞掉，所以這件事必須明確宣告，不能靠
# 直譯器版本。
mimetypes.add_type("text/javascript", ".mjs")


class _NoHeuristicCache(StaticFiles):
    """靜態檔一律 `Cache-Control: no-cache`。

    ⚠️ **這不是關掉快取**，是關掉「不問就用」。`no-cache` 的語意是
    「可以留著，但每次都要回來問」—— 沒改的話回 304，成本近乎零
    （這是 localhost）。

    為什麼非做不可：StaticFiles 只送 ETag 與 Last-Modified，**不送
    Cache-Control**。少了它，瀏覽器會套「啟發式快取」（依 Last-Modified
    推算一個保鮮期），在那段時間內連問都不問。

    症狀非常惡劣而且會誤導：`update.bat` 更新之後打開 GUI，載到的是**舊的
    JS 模組**，而畫面上一切正常 —— 只是新功能不見了、或新舊模組混在一起
    互相對不上。實測在開發時撞到三次，每次都先去懷疑程式碼。
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["cache-control"] = "no-cache"
        return resp
