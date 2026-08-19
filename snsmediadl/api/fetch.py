"""主動抓取端點（Misskey / Mastodon / pixiv）。

X 不走這裡 —— 它的資料來源是 extension。對 X 呼叫會回 400 並說明原因。

三個入口：
  POST /api/fetch          單一帳號（CLI 與既有呼叫端）
  POST /api/fetch/parse    貼一堆網址 -> 預覽（**不寫入任何東西**）
  POST /api/fetch/batch    確認送出 -> 排進佇列
  POST /api/fetch/refresh-all  一鍵更新 DB 裡已有的帳號

⚠️ 除了 `wait=True`（CLI 與測試要同步結果），全部走
`services/fetch_queue.py` 的**序列**佇列。併發列舉同一個站台是自找 429。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import IdListSource, get_source_adapter
from ..adapters.pixiv import DETAIL_DELAY_SECONDS
from ..config import Config
from ..db.models import Account
from ..services.fetch import fetch_account
from ..services.fetch_queue import plan_refresh
from ..urls import Target, parse_lines
from .app import get_config, get_fetch_queue, get_maker, get_session, get_transport

router = APIRouter(prefix="/api", tags=["fetch"])
log = logging.getLogger("snsmediadl")


# ── 單一帳號（既有介面）──────────────────────────────────


class FetchRequest(BaseModel):
    platform: str = Field(description="misskey / mastodon / pixiv")
    # pixiv 是單一站台，沒有 instance 的概念，所以這裡不是必填。
    host: str = Field(default="", description="instance，例如 misskey.io / baraag.net")
    acct: str = Field(description="帳號名稱，可帶 @。pixiv 要數字 user id")
    # 增量是預設行為，不是選項 —— full 只用在「補抓中間漏掉的」
    full: bool = False
    wait: bool = False


@router.post("/fetch")
async def post_fetch(
    body: FetchRequest, cfg: Config = Depends(get_config)
) -> dict:
    # 先驗參數再排背景工作 —— 否則 wait=False 時錯誤會掉進背景 task，
    # 呼叫端拿到 200 卻什麼都沒發生（典型的靜默失敗）。
    try:
        adapter = get_source_adapter(body.platform)
    except ValueError as exc:
        # X 走到這裡：它只能由 extension 推
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 游標式平台（Fediverse）沒有 host 是問不出東西的；
    # id 清單式平台（pixiv）本來就沒有 host。
    if not isinstance(adapter, IdListSource) and not body.host:
        raise HTTPException(
            status_code=400,
            detail=f"{body.platform} 需要 host（例如 misskey.io）",
        )

    target = Target(
        platform=body.platform, host=body.host, acct=body.acct.lstrip("@")
    )

    # wait=True 給 CLI 與測試用：要等結果，所以不進佇列直接跑。
    if body.wait:
        try:
            result = await fetch_account(
                cfg, get_maker(),
                platform=target.platform, host=target.host, acct=target.acct,
                full=body.full, transport=get_transport(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 要把原因回給呼叫端，不可以吞掉
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"started": True, "result": result.as_dict()}

    job = get_fetch_queue().enqueue(target, full=body.full)
    if job is None:
        return {"started": False, "already_running": True}
    return {"started": True, "already_running": False, "job_id": job.id}


# ── 批次 ─────────────────────────────────────────────────


class ParseRequest(BaseModel):
    text: str = Field(description="多行網址。空白行與 # 開頭會被略過")


def _existing_account(session: Session, target: Target) -> Account | None:
    stmt = select(Account).where(
        Account.platform == target.platform,
        Account.instance_host == target.host,
    )
    if target.platform == "pixiv":
        # pixiv 的 acct 就是穩定的 user id
        stmt = stmt.where(Account.platform_user_id == target.acct)
    else:
        # Fediverse 只有 handle 可比對（網址給不起 user id）。
        # 帳號改過名就會比不到 —— 那只影響「已在 DB」這個提示，
        # 抓下來仍然會靠 user id 正確去重，不會變成兩筆。
        stmt = stmt.where(Account.screen_name.ilike(target.acct))
    return session.scalars(stmt).first()


def missing_credential(cfg: Config, platform: str) -> str | None:
    """這個平台要憑證但沒設定嗎？回平台名（有問題）或 None。

    在**動手之前**就講。沒有這個判斷，沒填 PHPSESSID 的人會先抓一輪、
    再從佇列深處撈出一個失敗訊息 —— 而那個失敗長得像 Cloudflare 擋人
    （403 挑戰頁），完全看不出真正的原因是「沒填憑證」。
    """
    if platform != "pixiv":
        return None
    return None if (cfg.platform_credentials or {}).get("pixiv") else "pixiv"


@router.post("/fetch/parse")
def post_parse(
    body: ParseRequest,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> dict:
    """解析多行網址。**只讀，不寫入任何東西。**

    存在的理由跟刪除功能的預演一樣：打錯字不該直接變成一筆垃圾帳號記錄，
    而批次的錯誤若混在執行過程才冒出來，沒有人會看。
    """
    out = []
    for line in parse_lines(body.text):
        item: dict = {
            "raw": line.raw,
            "duplicate": line.duplicate,
            "error": line.error,
            # 'x' / 'instagram'：貼對了但要換工具。介面要把它與「打錯字」
            # 分成兩種結論 —— 正式庫 90.5% 的帳號是 X。
            "unsupported_platform": line.unsupported_platform,
            # 這一行看得懂、平台也抓得動，但**憑證還沒設**。
            # 與 error 是兩件事：這行沒有錯，只是現在跑會失敗。
            "needs_credential": None,
        }
        if line.target is not None:
            item["needs_credential"] = missing_credential(cfg, line.target.platform)
            existing = _existing_account(session, line.target)
            item["target"] = {
                "platform": line.target.platform,
                "host": line.target.host,
                "acct": line.target.acct,
                "label": line.target.label,
            }
            item["in_db"] = existing is not None
            item["account_id"] = existing.id if existing else None
        out.append(item)
    return {"lines": out}


class BatchRequest(BaseModel):
    text: str
    full: bool = False
    # 抓完就下載。**明確觸發（queue/run），不碰 auto_download** ——
    # 那個開關管的是每 5 秒自撿的背景迴圈，是另一件事。
    download: bool = True


@router.post("/fetch/batch")
async def post_batch(body: BatchRequest) -> dict:
    """確認送出。在**伺服器端重新解析**，不信任前端送回來的解析結果。"""
    queue = get_fetch_queue()
    queued: list[dict] = []
    rejected: list[dict] = []
    already: list[str] = []

    for line in parse_lines(body.text):
        if line.error is not None:
            rejected.append({"raw": line.raw, "error": line.error})
            continue
        if line.duplicate:
            continue        # 批內重複，parse 已經標過了
        assert line.target is not None
        job = queue.enqueue(
            line.target, full=body.full, download_after=body.download
        )
        if job is None:
            already.append(line.target.label)
        else:
            queued.append(job.as_dict())

    return {
        "queued": len(queued),
        "jobs": queued,
        "rejected": rejected,
        "already_queued": already,
    }


class RefreshRequest(BaseModel):
    full: bool = False
    download: bool = True
    # pixiv 一個帳號可能跑很久（1800ms 間隔 + 併發 1），
    # 混在 Fediverse 的批次裡會讓人以為當掉了，所以預設不含。
    include_pixiv: bool = False


@router.post("/fetch/refresh-all")
async def post_refresh_all(
    body: RefreshRequest,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> dict:
    """一鍵更新：把 DB 裡抓得動的帳號全部跑一次增量。

    「只抓最新（尚未抓取）」不需要新演算法 —— `fetch_account(full=False)`
    碰到已知貼文就停，對已經跟上的帳號只會發 1 個請求。

    ⚠️ **跳過的要逐類講出來。** 只回一個數字的話，使用者會以為
    X 的帳號也更新過了。
    """
    queue = get_fetch_queue()
    plan = plan_refresh(session, cfg, include_pixiv=body.include_pixiv)
    queued: list[dict] = []

    for target, user_id in plan.targets:
        job = queue.enqueue(
            target,
            full=body.full,
            download_after=body.download,
            # ⚠️ 用平台 user id 解析，不用 screen_name —— 帳號改名是常態，
            # 拿舊名字去查會 404，那個帳號從此再也更新不到。
            user_id=user_id,
        )
        if job is None:
            plan.skip("already_queued", target.label)
        else:
            queued.append(job.as_dict())

    return {
        "queued": len(queued),
        "jobs": queued,
        "skipped": plan.skipped,
        "skipped_counts": {k: len(v) for k, v in plan.skipped.items()},
    }


@router.get("/fetch/refresh-preview")
def get_refresh_preview(
    include_pixiv: bool = False,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> dict:
    """「一鍵更新」按下去會跑哪些帳號。**不排任何東西。**

    存在的理由：一個以 X 為主的媒體庫裡，**多數帳號 backend 抓不動**
    （X 的公開 API 已關，只能由 extension 在登入的頁面裡採集）。作者自己的
    庫是 4,653 個帳號裡 4,211 個（90.5%）。那不是邊緣情況，是多數情況，
    所以必須在按下去**之前**就講出來，而不是送出後才逐筆報「跳過」。

    ⚠️ 走的是與真正執行時**同一個** `plan_refresh`。自己在這裡重寫一次分類
    邏輯的話，兩邊會漂移，畫面上的「可抓 N 個」就會變成謊話。

    `min_seconds` 是**下限**不是預估：每個帳號至少要一個請求，pixiv 的每個
    請求至少 1.8 秒。實際上有新作品時每一件都要一個請求，會久很多 ——
    介面必須照這個語意寫（「至少」），不可以拿它當完成時間。
    """
    plan = plan_refresh(session, cfg, include_pixiv=include_pixiv)
    by_platform: dict[str, int] = {}
    for target, _user_id in plan.targets:
        by_platform[target.platform] = by_platform.get(target.platform, 0) + 1

    min_seconds = sum(
        (DETAIL_DELAY_SECONDS if platform == "pixiv" else cfg.fetch_delay_seconds) * n
        for platform, n in by_platform.items()
    )
    return {
        "fetchable": len(plan.targets),
        "by_platform": by_platform,
        "skipped": {k: len(v) for k, v in plan.skipped.items()},
        "min_seconds": round(min_seconds),
    }


# ── 佇列狀態 ─────────────────────────────────────────────


@router.get("/fetch/queue")
def get_queue() -> dict:
    return get_fetch_queue().status()


@router.delete("/fetch/queue")
def delete_queue() -> dict:
    """清掉還沒開始的。正在跑的那一筆讓它跑完 ——
    中途砍掉會留下一半的列舉結果，而且沒辦法標示。"""
    return {"cleared": get_fetch_queue().clear_pending()}


# ── 重試與續抓 ───────────────────────────────────────────
#
# ⚠️ 全部寫成 `async def`。`enqueue()` 會碰 `asyncio.Event.set()`，而 FastAPI
# 會把 **sync def** 的端點丟到 threadpool —— 那裡沒有 running loop，
# 而 `Event.set()` 也不是 thread-safe（見 `fetch_queue.start()` 的說明）。


class RetryAllRequest(BaseModel):
    # 站台被限速那一輪被跳過的，**根本沒跑過** —— 預設納入，它是重試最主要
    # 的服務對象。
    include_skipped: bool = True
    # 缺憑證 / 找不到。預設**不**納入：原因沒排除，重試必定同樣失敗。
    # 單筆重試不受這個限制（那是使用者覆寫）。
    include_unretryable: bool = False
    # 預設 False。自動解除等於用 fallback 掩蓋「對方在擋我們」，而且解除
    # 窗口我們不知道，猜錯就是再撞一次 —— Fediverse 的 429 政策是停止不重試。
    clear_rate_limit: bool = False


@router.post("/fetch/queue/retry-failed")
async def post_retry_failed(body: RetryAllRequest) -> dict:
    """把歷史裡可重試的全部重排。

    回應的 `will_be_skipped` **一定要用**：站台旗標還掛著時，重排的 job 會在
    `_process()` 開頭第一行就被標成 skipped。不講的話，使用者會看到
    「已排入 201 個」然後幾秒內全部變 ⊘，而完全不知道發生了什麼。

    `refused` 同理 —— 該帳號已經在佇列裡時 `enqueue()` 回 None，
    靜默少排幾個就是這個專案禁止的靜默漏抓。
    """
    return get_fetch_queue().retry_failed(
        include_skipped=body.include_skipped,
        include_unretryable=body.include_unretryable,
        clear_rate_limit=body.clear_rate_limit,
    )


@router.post("/fetch/queue/resume-capped")
async def post_resume_capped() -> dict:
    """把撞到頁數上限的全部續抓。

    ⚠️ 與重試是**兩個**動作。撞上限不是失敗（job.state 是 done），
    硬塞進「重試失敗的」會讓失敗分類的明細說謊。
    """
    return get_fetch_queue().resume_capped()


@router.post("/fetch/queue/{job_id}/retry")
async def post_retry_one(job_id: int) -> dict:
    """重試單一筆。**不可重試的類別也允許** —— 這是使用者覆寫。

    使用者剛去設定頁填完 PHPSESSID、剛在帳號頁確認過改名時，他知道的比
    系統多。做成 disabled 會擋掉唯一真正合理的使用情境。
    """
    return get_fetch_queue().retry(job_id)


@router.post("/fetch/queue/{job_id}/resume")
async def post_resume_one(job_id: int) -> dict:
    """從上次撞上限的游標接下去。**不是**從第 1 頁重來。"""
    return get_fetch_queue().retry(job_id, resume=True)


@router.delete("/fetch/rate-limit")
def clear_rate_limit() -> dict:
    """手動解除限速標記。

    不自動解除是刻意的：那需要知道對方的限速窗口有多長，我們不知道，
    猜錯就是再撞一次 —— 而 Fediverse 的 429 政策是停止不重試。
    """
    get_fetch_queue().clear_rate_limit()
    return {"cleared": True}
