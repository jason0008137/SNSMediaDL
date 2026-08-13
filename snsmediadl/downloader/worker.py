"""非同步下載 worker。

設計要點：
  - 先寫 `.part`，完成才 `os.replace` —— 中斷不會留下半截的正式檔
  - hash 在串流過程中算，不需要讀第二遍
  - DB 操作走 `asyncio.to_thread`，維持 ORM 同步的簡單性
  - 4xx 是永久失敗，立刻放棄；網路錯誤與 5xx 才重試
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..adapters import MediaUrlRepair, get_adapter
from ..config import Config
from ..db.enums import MediaStatus
from ..db.models import Account, Media, Post
from ..naming import build_target_path, build_tokens, resolve_collision

CHUNK = 64 * 1024

log = logging.getLogger("snsmediadl.downloader")


@dataclass
class WorkItem:
    """從 DB 撈出來的純資料。ORM 物件不跨 thread。"""

    media_id: int
    platform: str
    post_id: str
    ordinal: int
    kind: str
    source_url: str
    posted_at: datetime | None
    user_id: str
    screen_name: str
    known_path: str | None
    known_hash: str | None
    attempt_count: int
    # 平台的穩定媒體鍵。網址推導失效時要靠它去校正（目前只有 pixiv 有）。
    platform_media_key: str | None = None


@dataclass
class WorkerStats:
    done: int = 0
    failed: int = 0
    skipped: int = 0
    # 被平台限速而中止。這不是「失敗」—— 檔案沒壞，只是現在不能抓。
    rate_limited: bool = False
    # ⚠️ 限速是**平台層**的事實，不是整輪的。
    # `_load_pending` 撈的是所有平台混在一起的 pending，只有一個布林的話，
    # pixiv 被限速會連帶停掉 X 的下載（反之亦然）——那是資料處理錯誤，
    # 不是顯示問題：使用者會以為 X 抓完了，其實只是被別的平台牽連。
    rate_limited_platforms: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
            "rate_limited": self.rate_limited,
            "rate_limited_platforms": sorted(self.rate_limited_platforms),
            "errors": self.errors,
        }


class Throttle:
    """全域的下載起始節流。

    只有 semaphore（同時幾個）是不夠的：四個並行工作一完成就立刻抓下一批，
    實際速率只受頻寬限制。X 超速會鎖整個帳號約一天，所以還要限制
    「任兩次下載開始的最小間隔」。
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._lock = asyncio.Lock()
        self._last_start = 0.0

    async def wait(self) -> None:
        if not self.min_interval:
            return
        async with self._lock:
            now = asyncio.get_running_loop().time()
            gap = now - self._last_start
            if gap < self.min_interval:
                await asyncio.sleep(self.min_interval - gap)
            self._last_start = asyncio.get_running_loop().time()


def _load_pending(session: Session, limit: int | None) -> list[WorkItem]:
    stmt = (
        select(Media, Post, Account)
        .join(Post, Media.post_id == Post.id)
        .join(Account, Post.account_id == Account.id)
        .where(Media.status == MediaStatus.PENDING.value)
        .order_by(Media.id)
    )
    if limit:
        stmt = stmt.limit(limit)

    return [
        WorkItem(
            media_id=m.id,
            platform=p.platform,
            post_id=p.platform_post_id,
            ordinal=m.ordinal,
            kind=m.kind,
            source_url=m.source_url,
            posted_at=p.posted_at,
            user_id=a.platform_user_id,
            screen_name=a.screen_name or a.platform_user_id,
            known_path=m.local_path,
            known_hash=m.file_hash,
            attempt_count=m.attempt_count,
            platform_media_key=m.platform_media_key,
        )
        for m, p, a in session.execute(stmt).all()
    ]


def _mark(
    maker: sessionmaker[Session],
    media_id: int,
    *,
    status: str,
    local_path: str | None = None,
    file_hash: str | None = None,
    size: int | None = None,
    error: str | None = None,
    add_attempts: int = 0,
    source_url: str | None = None,
) -> None:
    with maker() as s:
        media = s.get(Media, media_id)
        if media is None:  # pragma: no cover - 只有並行刪除才會發生
            return
        media.status = status
        if source_url is not None:
            # 只有網址校正會走到這裡（見 _download_one 的 404 分支）
            media.source_url = source_url
        if local_path is not None:
            media.local_path = local_path
        if file_hash is not None:
            media.file_hash = file_hash
        if size is not None:
            media.bytes = size
        media.error = error
        # 記錄實際發出的 HTTP 次數，不是 worker 跑過幾輪 ——
        # 「這個檔試過幾次」才是判斷該不該放棄的依據。
        media.attempt_count += add_attempts
        if status == MediaStatus.DONE.value:
            media.downloaded_at = datetime.now(timezone.utc)
        s.commit()


async def _mark_async(lock: asyncio.Lock, *args, **kwargs) -> None:
    """所有 DB 寫入都走這裡。

    SQLite 本來就只允許一個寫入者，序列化寫入不會損失吞吐（慢的是下載），
    卻能避免多個 session 同時提交造成的競態。
    """
    async with lock:
        await asyncio.to_thread(_mark, *args, **kwargs)


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _already_downloaded(item: WorkItem) -> bool:
    """檔案還在且 hash 對得上 -> 不重抓。

    兩個條件缺一不可：只有路徑沒有 hash 時無法確認內容完整，寧可重抓。
    """
    if not item.known_path or not item.known_hash:
        return False
    path = Path(item.known_path)
    if not path.exists():
        return False
    return _hash_file(path) == item.known_hash


async def _repair_url(
    cfg: Config,
    adapter: MediaUrlRepair,
    item: WorkItem,
    transport: httpx.AsyncBaseTransport | None,
) -> str:
    """問平台這個媒體真正的網址。

    另開一個 client 而不是重用下載用的那個：校正打的是平台 API
    （`www.pixiv.net`，要憑證），下載打的是 CDN（`i.pximg.net`，**不要憑證**）。
    這兩者的 header 不能混。
    """
    headers = {"User-Agent": "SNSMediaDL/0.1"}
    headers.update(adapter.auth_headers(cfg, ""))
    async with httpx.AsyncClient(
        transport=transport, timeout=cfg.timeout_seconds, headers=headers
    ) as api_client:
        return await adapter.repair_media_url(
            api_client, item.platform_media_key or "", item.source_url
        )


async def _download_one(
    client: httpx.AsyncClient,
    cfg: Config,
    maker: sessionmaker[Session],
    item: WorkItem,
    stats: WorkerStats,
    lock: asyncio.Lock,
    db_lock: asyncio.Lock,
    throttle: Throttle,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    # 已經被限速了就別再送 —— 繼續打正是把「暫時限速」變成「帳號鎖定」的方式。
    # 依平台判斷：別的平台被限速不該影響這個平台。
    if item.platform in stats.rate_limited_platforms:
        return

    if await asyncio.to_thread(_already_downloaded, item):
        await _mark_async(db_lock, maker, item.media_id, status=MediaStatus.DONE.value)
        stats.skipped += 1
        # 略過的沒有打網路，不該佔用節流額度
        return

    adapter = get_adapter(item.platform)

    def _tokens() -> dict[str, str]:
        # 包成函式是因為網址校正後要重算 —— `%ext%` 是從 source_url 推的，
        # 校正後副檔名可能不一樣，沿用舊的會存出錯的檔名。
        return build_tokens(
            platform=item.platform,
            post_id=item.post_id,
            ordinal=item.ordinal,
            kind=item.kind,
            source_url=item.source_url,
            posted_at=item.posted_at,
            user_id=item.user_id,
            screen_name=item.screen_name,
        )

    tokens = _tokens()

    await _mark_async(
        db_lock, maker, item.media_id, status=MediaStatus.DOWNLOADING.value
    )

    last_error: str | None = None
    # 記錄實際發出的 HTTP 次數（含 429 重試與校正後的重試），
    # 與「用掉第幾次一般嘗試」分開算 —— 前者才是 attempt_count 的語意。
    http_calls = 0
    rate_limit_retries = 0
    url_repaired = False
    attempt = 0
    while attempt < cfg.max_attempts:
        attempt += 1
        http_calls += 1
        await throttle.wait()
        try:
            target, digest, size = await _stream_to_disk(
                client, cfg, adapter, item, tokens, lock
            )
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 429:
                # 429 的處置是**平台屬性**，不是全域設定：
                # X 超速會鎖整個帳號約一天（停止不重試）；
                # pixiv 是可恢復的暫時限制（等 200 秒再試）。
                policy = adapter.rate_limit_policy
                retry_after = exc.response.headers.get("retry-after")

                if rate_limit_retries < policy.max_retries:
                    rate_limit_retries += 1
                    attempt -= 1  # 被限速不算「這個檔失敗了一次」
                    log.warning(
                        "%s 限速（HTTP 429%s）—— 等 %.0f 秒後重試（第 %s/%s 次）",
                        item.platform,
                        f"，Retry-After: {retry_after}" if retry_after else "",
                        policy.wait_seconds,
                        rate_limit_retries,
                        policy.max_retries,
                    )
                    await asyncio.sleep(policy.wait_seconds)
                    continue

                # 重試用完了還是 429 = 真的被限速。
                # 不標 failed —— 檔案沒壞，只是現在不能抓。
                msg = f"{item.platform} 已被平台限速（HTTP 429）"
                if retry_after:
                    msg += f"，Retry-After: {retry_after}"
                if policy.max_retries:
                    msg += f"（已重試 {rate_limit_retries} 次仍失敗）"
                stats.rate_limited = True
                stats.rate_limited_platforms.add(item.platform)
                await _mark_async(
                    db_lock, maker, item.media_id,
                    status=MediaStatus.PENDING.value,
                    error=msg,
                    add_attempts=http_calls,
                )
                log.error("%s —— 已停止本輪 %s 的下載，請稍後再試", msg, item.platform)
                stats.errors.append(msg)
                return

            if (
                code == 404
                and not url_repaired
                and item.platform_media_key
                and isinstance(adapter, MediaUrlRepair)
            ):
                # 這個網址是**推導**出來的（pixiv 多頁作品的 _p0 → _pN）。
                # 404 代表推導可能失效，去問平台真正的網址。
                #
                # ⚠️ 這不是「找不到就退而求其次」的兜底 —— 校正成功也是 WARNING，
                # 因為它代表 pximg 的 URL 樣式變了，是需要有人知道的事實。
                url_repaired = True
                try:
                    fixed = await _repair_url(cfg, adapter, item, transport)
                except Exception as repair_exc:  # noqa: BLE001 - 原因要留下來
                    last_error = f"HTTP 404，且網址校正失敗：{repair_exc}"
                    log.error("%s（media#%s）", last_error, item.media_id)
                    break
                if fixed != item.source_url:
                    log.warning(
                        "⚠️ %s 的網址推導失效並已校正：%s -> %s"
                        "（pximg 的 URL 樣式可能改了，請重新確認 recon 筆記）",
                        item.platform_media_key, item.source_url, fixed,
                    )
                    stats.errors.append(
                        f"{item.platform_media_key} 網址推導失效，已校正後重試"
                    )
                    item.source_url = fixed
                    tokens = _tokens()
                    await _mark_async(
                        db_lock, maker, item.media_id,
                        status=MediaStatus.DOWNLOADING.value,
                        source_url=fixed,
                    )
                    attempt -= 1  # 校正後的重試不算「這個檔失敗了一次」
                    continue
                last_error = "HTTP 404（校正後網址相同，作品可能已被刪除）"
                break

            last_error = f"HTTP {code}"
            if 400 <= code < 500:
                break  # 永久失敗，重試沒有意義
        except (httpx.HTTPError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            await _mark_async(
                db_lock,
                maker,
                item.media_id,
                status=MediaStatus.DONE.value,
                local_path=str(target),
                file_hash=digest,
                size=size,
                add_attempts=http_calls,
            )
            stats.done += 1
            return

        if attempt < cfg.max_attempts:
            await asyncio.sleep(0.2 * attempt)

    await _mark_async(
        db_lock,
        maker,
        item.media_id,
        status=MediaStatus.FAILED.value,
        error=last_error,
        add_attempts=http_calls,
    )
    stats.failed += 1
    stats.errors.append(f"media#{item.media_id}: {last_error}")


async def _stream_to_disk(
    client: httpx.AsyncClient,
    cfg: Config,
    adapter,
    item: WorkItem,
    tokens: dict[str, str],
    lock: asyncio.Lock,
) -> tuple[Path, str, int]:
    headers = adapter.download_headers(item.source_url)

    async with client.stream(
        "GET", item.source_url, headers=headers, follow_redirects=True
    ) as response:
        response.raise_for_status()

        # 目標路徑要在確定會寫入時才決定，且加鎖避免兩個工作挑到同一個名字
        async with lock:
            target = build_target_path(
                output_root=cfg.output_root,
                fmt=cfg.filename_format,
                tokens=tokens,
                group_by_account=cfg.group_by_account,
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target = resolve_collision(target)
            part = target.with_suffix(target.suffix + ".part")
            part.touch()

        digest = hashlib.sha256()
        size = 0
        try:
            with part.open("wb") as fh:
                async for chunk in response.aiter_bytes(CHUNK):
                    fh.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            os.replace(part, target)
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    return target, digest.hexdigest(), size


async def run_worker(
    cfg: Config,
    maker: sessionmaker[Session],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    limit: int | None = None,
) -> WorkerStats:
    """把佇列裡 pending 的媒體全部抓完。

    `transport` 用來在測試裡塞 MockTransport —— 測試不打網路。
    """
    with maker() as session:
        items = _load_pending(session, limit)

    stats = WorkerStats()
    if not items:
        return stats

    cfg.output_root.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(cfg.concurrency)
    name_lock = asyncio.Lock()
    db_lock = asyncio.Lock()
    throttle = Throttle(cfg.download_delay_seconds)

    async with httpx.AsyncClient(
        transport=transport, timeout=cfg.timeout_seconds
    ) as client:

        async def guarded(item: WorkItem) -> None:
            async with semaphore:
                await _download_one(
                    client, cfg, maker, item, stats, name_lock, db_lock, throttle,
                    transport,
                )

        await asyncio.gather(*(guarded(i) for i in items))

    return stats
