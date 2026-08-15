"""抓取佇列。**一次一個帳號，序列跑。**

⚠️ 為什麼不併發：
先前 `POST /api/fetch` 每呼叫一次就 `asyncio.create_task`。批次 20 個帳號
＝ 20 條列舉同時打同一個 instance，那是自己把自己打成 429 ——
而 Fediverse 的 429 政策是 `CONSERVATIVE_RATE_LIMIT`（停止、不重試），
撞上去就是整批停掉。列舉不趕時間，序列跑。

⚠️ 限速旗標**依站台隔離**，不可以用一個全域布林。
既有教訓：`WorkerStats.rate_limited` 曾讓 pixiv 的 429 停掉 X 的下載，
使用者會以為 X 抓完了。同一個錯不要犯第二次。

⚠️ 佇列只在記憶體，重啟就沒了。這是刻意的 —— 增量讓「重跑一次」的成本
接近零（跟上的帳號只發 1 個請求），為此加一張表與 migration 不划算。
但 `status()` 會回 `volatile: True`，介面要講明。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from sqlalchemy import select

from ..adapters import AuthRequired, IdListSource, SourceAdapter, get_adapter
from ..config import Config
from ..db.enums import FetchStatus
from ..db.models import Account
from ..urls import Target
from .fetch import fetch_account

log = logging.getLogger("snsmediadl.fetch")

# 已結束的工作保留幾筆給介面看。批次跑完使用者要能逐筆檢查，
# 但不需要無限累積。
_HISTORY_LIMIT = 200


def can_fetch(platform: str) -> bool:
    """backend 抓不抓得動這個平台。X 抓不動 —— 它的公開 API 已死。"""
    try:
        adapter = get_adapter(platform)
    except ValueError:
        return False
    return isinstance(adapter, (SourceAdapter, IdListSource))


@dataclass
class RefreshPlan:
    """一鍵更新要做什麼、以及**為什麼跳過那些**。

    ⚠️ 跳過的理由一定要逐類帶出來。只回一個數字的話，使用者會以為
    X 的帳號也更新過了 —— 那是靜默漏抓的另一種形狀。
    """

    # (目標, 平台 user id)
    targets: list[tuple[Target, str]] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)

    def skip(self, reason: str, label: str) -> None:
        self.skipped.setdefault(reason, []).append(label)


def account_label(acc: Account) -> str:
    name = acc.screen_name or acc.platform_user_id
    return f"@{name}@{acc.instance_host}" if acc.instance_host else f"{acc.platform}:{name}"


def plan_refresh(session: Any, cfg: Config, *, include_pixiv: bool = False) -> RefreshPlan:
    """挑出「一鍵更新」要跑哪些帳號。API 與 CLI 共用，避免兩套分類漂移。"""
    plan = RefreshPlan()
    for acc in session.scalars(select(Account).order_by(Account.id)).all():
        label = account_label(acc)
        if not acc.is_tracked:
            plan.skip("untracked", label)
        elif not can_fetch(acc.platform):
            plan.skip("cannot_fetch", label)
        elif acc.platform == "pixiv" and not include_pixiv:
            # pixiv 一個帳號可能跑很久（1800ms 間隔 + 併發 1），
            # 混在 Fediverse 的批次裡會讓人以為當掉了
            plan.skip("pixiv_excluded", label)
        elif acc.platform == "pixiv" and not (cfg.platform_credentials or {}).get("pixiv"):
            plan.skip("no_credentials", label)
        else:
            target = Target(
                platform=acc.platform,
                host=acc.instance_host,
                acct=acc.screen_name or acc.platform_user_id,
            )
            plan.targets.append((target, acc.platform_user_id))
    return plan


@dataclass
class Job:
    id: int
    platform: str
    host: str
    acct: str
    full: bool = False
    # 有值就用平台 user id 解析（更新既有帳號一律帶，因為帳號會改名）
    user_id: str | None = None
    # queued | running | done | failed | skipped
    state: str = "queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    reason: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.platform, self.host, self.acct.lower())

    @property
    def label(self) -> str:
        return f"@{self.acct}@{self.host}" if self.host else f"{self.platform}:{self.acct}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "platform": self.platform,
            "host": self.host,
            "acct": self.acct,
            "label": self.label,
            "full": self.full,
            "state": self.state,
            "result": self.result,
            "error": self.error,
            "reason": self.reason,
        }


@dataclass
class FetchQueue:
    cfg: Config
    maker: sessionmaker[Session]
    transport: httpx.AsyncBaseTransport | None = None
    # 佇列跑空時呼叫（用來觸發下載）。**明確觸發，不碰 auto_download。**
    on_drain: Callable[[], Awaitable[None]] | None = None
    # CLI 用 `run_all()` 自己跑完，不要背景 worker 來搶同一批工作
    autostart: bool = True

    _pending: deque[Job] = field(default_factory=deque, init=False)
    _history: deque[Job] = field(default_factory=deque, init=False)
    _running: Job | None = field(default=None, init=False)
    # 已排入或正在跑的帳號，避免同一個帳號排兩次（兩次會列舉同一批，純浪費額度）
    _active: set[tuple[str, str, str]] = field(default_factory=set, init=False)
    _rate_limited: dict[tuple[str, str], str] = field(default_factory=dict, init=False)
    _want_download: bool = field(default=False, init=False)
    _next_id: int = field(default=1, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _task: asyncio.Task | None = field(default=None, init=False)

    # ── 對外 ─────────────────────────────────────────────

    def enqueue(
        self,
        target: Target,
        *,
        full: bool = False,
        download_after: bool = False,
        user_id: str | None = None,
    ) -> Job | None:
        """排一個帳號。已經在跑或已在佇列裡就回 None（不重複排）。"""
        key = (target.platform, target.host, target.acct.lower())
        if key in self._active:
            return None

        job = Job(
            id=self._next_id,
            platform=target.platform,
            host=target.host,
            acct=target.acct,
            full=full,
            user_id=user_id,
        )
        self._next_id += 1
        self._active.add(key)
        self._pending.append(job)
        if download_after:
            self._want_download = True

        # 懶啟動：不依賴 lifespan 有沒有跑起來（TestClient 不進 context
        # 時就不會跑 lifespan），也不需要在沒有 event loop 時建 task。
        self._ensure_worker()
        self._wake.set()
        return job

    def clear_pending(self) -> int:
        """清掉還沒開始的。**正在跑的那一筆讓它跑完** ——
        中途砍掉會留下一半的列舉結果，而且沒有辦法標示。"""
        n = len(self._pending)
        for job in self._pending:
            self._active.discard(job.key)
            job.state = "skipped"
            job.reason = "使用者清空佇列"
            self._remember(job)
        self._pending.clear()
        self._want_download = False
        return n

    def status(self) -> dict[str, Any]:
        history = list(self._history)
        counts = {
            "queued": len(self._pending),
            "running": 1 if self._running else 0,
            "done": sum(1 for j in history if j.state == "done"),
            "failed": sum(1 for j in history if j.state == "failed"),
            "skipped": sum(1 for j in history if j.state == "skipped"),
        }
        return {
            "running": self._running.as_dict() if self._running else None,
            "queued": [j.as_dict() for j in self._pending],
            "recent": [j.as_dict() for j in reversed(history)],
            "counts": counts,
            # 站台 -> 原因。介面要能說出「為什麼這些被跳過」
            "rate_limited": {f"{p}@{h}" if h else p: why
                             for (p, h), why in self._rate_limited.items()},
            # 重啟就沒了，介面要講明
            "volatile": True,
        }

    def start(self) -> None:
        """在有 event loop 的地方先把 worker 起來（lifespan 會呼叫）。

        ⚠️ `enqueue()` 必須在 event loop 上呼叫。FastAPI 會把 **sync def**
        的端點丟到 threadpool，那裡沒有 running loop，`asyncio.Event.set()`
        也不是 thread-safe —— 所以排入佇列的端點一律寫成 `async def`。
        """
        self._ensure_worker()

    def clear_rate_limit(self) -> None:
        """使用者等過一段時間後手動解除。自動解除需要知道對方的窗口長度，
        我們不知道，猜錯就是再撞一次。"""
        self._rate_limited.clear()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    # ── 內部 ─────────────────────────────────────────────

    async def run_all(self) -> list[Job]:
        """把目前排著的全部跑完再回來。CLI 用（要照順序印結果與決定 exit code）。

        與背景 worker 共用 `_process()`，所以 429 隔離、逐筆錯誤處理
        只有一份實作，不會漂移。
        """
        done: list[Job] = []
        while self._pending:
            job = self._pending.popleft()
            await self._process(job)
            done.append(job)
        await self._drain()
        return done

    def _ensure_worker(self) -> None:
        if not self.autostart:
            return
        if self._task is not None and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 沒有 event loop（同步情境下排入）。下次在 loop 裡排時會補起來。
            self._task = None
            return
        self._task = loop.create_task(self._run())

    def _remember(self, job: Job) -> None:
        self._history.append(job)
        while len(self._history) > _HISTORY_LIMIT:
            self._history.popleft()

    async def _run(self) -> None:
        while True:
            if not self._pending:
                self._wake.clear()
                await self._wake.wait()
                continue

            job = self._pending.popleft()
            await self._process(job)

            if not self._pending:
                await self._drain()

    async def _process(self, job: Job) -> None:
        limited = self._rate_limited.get((job.platform, job.host))
        if limited:
            job.state = "skipped"
            job.reason = limited
            self._active.discard(job.key)
            self._remember(job)
            # 跳過也算「這一輪碰過它了」—— 不記的話使用者看到的是
            # 一個很久沒被檢查的帳號，卻不知道原因是站台被限速。
            await self._record(job, FetchStatus.SKIPPED.value, limited, None)
            return

        job.state = "running"
        self._running = job
        try:
            result = await fetch_account(
                self.cfg, self.maker,
                platform=job.platform, host=job.host, acct=job.acct,
                full=job.full, user_id=job.user_id, transport=self.transport,
            )
            job.state = "done"
            job.result = result.as_dict()
            # ok 與 no_new 分開：兩者都成功，但使用者要知道的是「有沒有
            # 東西進來」。合成一個 ok 等於把答案藏起來。
            status = (FetchStatus.OK.value if result.posts_new
                      else FetchStatus.NO_NEW.value)
            outcome = (status, result.stopped_because, result.posts_new)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            job.state = "failed"
            if code == 429:
                why = "這個站台稍早回了 429（限速）—— 等一陣子再手動解除"
                self._rate_limited[(job.platform, job.host)] = why
                job.error = f"被限速（429）：{job.label}"
                outcome = (FetchStatus.RATE_LIMITED.value, job.error, None)
            elif code == 404:
                # 最常見的原因是帳號改名或打錯字。講出來，否則使用者只看到 404
                job.error = f"找不到 {job.label}（404）—— 帳號可能改名或打錯字"
                outcome = (FetchStatus.NOT_FOUND.value, job.error, None)
            else:
                job.error = f"HTTP {code}：{job.label}"
                outcome = (FetchStatus.FAILED.value, job.error, None)
        except AuthRequired as exc:
            job.state = "failed"
            job.error = str(exc)
            outcome = (FetchStatus.AUTH_REQUIRED.value, job.error, None)
        except Exception as exc:  # noqa: BLE001 - 一筆失敗不可以停掉整批
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            outcome = (FetchStatus.FAILED.value, job.error, None)
        finally:
            self._running = None
            self._active.discard(job.key)
            self._remember(job)

        # 失敗一樣要記 —— 那正是最需要被看見的一種。
        await self._record(job, *outcome)

        if job.state == "failed":
            log.error("抓取失敗 %s：%s", job.label, job.error)

    async def _record(
        self, job: Job, status: str, note: str | None, new_posts: int | None
    ) -> None:
        """把這一輪的結果寫回 accounts。

        **用自己的 session**，不搭 `fetch_account` 的便車：失敗路徑上那個
        session 可能已經 rollback，而失敗恰恰是最需要被記下來的那一種。
        """
        def _write() -> None:
            with self.maker() as session:
                stmt = select(Account).where(
                    Account.platform == job.platform,
                    Account.instance_host == job.host,
                )
                # 有 user_id 就用它 —— 帳號會改名，screen_name 不是穩定鍵
                if job.user_id:
                    stmt = stmt.where(Account.platform_user_id == job.user_id)
                else:
                    stmt = stmt.where(Account.screen_name == job.acct)
                acc = session.scalars(stmt).first()
                if acc is None:
                    # 出聲，不要靜默跳過。找不到通常代表 ingest 把它建成了
                    # 另一列（例如 Fediverse 的 handle 被解析成真實數字 id，
                    # 而 DB 裡原本是 `sn:` 暫代的那一列）—— 那是真的問題。
                    log.warning(
                        "記錄擷取結果時找不到帳號：%s（user_id=%s）",
                        job.label, job.user_id,
                    )
                    return
                acc.last_fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
                acc.last_fetch_status = status
                acc.last_fetch_note = (note or None)
                acc.last_fetch_new_posts = new_posts
                session.commit()

        try:
            await asyncio.to_thread(_write)
        except Exception:  # noqa: BLE001 - 記錄失敗不可以連帶弄垮抓取
            log.exception("寫入擷取記錄失敗：%s", job.label)

    async def _drain(self) -> None:
        if not self._want_download:
            return
        self._want_download = False
        if self.on_drain is None:
            return
        try:
            await self.on_drain()
        except Exception:  # noqa: BLE001 - 觸發下載失敗不該讓 worker 死掉
            log.exception("批次抓取結束後觸發下載失敗")
