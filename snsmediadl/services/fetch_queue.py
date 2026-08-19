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
from ..adapters.pixiv import PixivNotFound
from ..config import Config
from ..db.enums import FetchStatus
from ..db.models import Account
from ..urls import Target
from .fetch import fetch_account
from .identity import is_placeholder

log = logging.getLogger("snsmediadl.fetch")

# 已結束的工作保留幾筆給介面看。批次跑完使用者要能逐筆檢查，
# 但不需要無限累積。
_HISTORY_LIMIT = 200


def _error_detail(response: httpx.Response) -> str:
    """從錯誤回應裡挖出平台自己講的原因。挖不到就回空字串。

    Misskey：`{"error": {"code": "...", "message": "..."}}`
    Mastodon：`{"error": "...", "error_description": "..."}`

    ⚠️ 這個函式**不可以拋例外**。它跑在錯誤處理路徑上，body 可能不是 JSON、
    可能是一整頁 HTML、也可能是空的 —— 在這裡再炸一次會把原本那個
    HTTPStatusError 蓋掉，使用者連狀態碼都看不到。
    """
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 不是 JSON 就退回原始文字
        text = (response.text or "").strip()
        return text[:200]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            parts = [str(err.get(k)) for k in ("code", "message") if err.get(k)]
            return "：".join(parts)[:200]
        if err:
            desc = body.get("error_description")
            return f"{err}{f'：{desc}' if desc else ''}"[:200]
    return str(body)[:200]


# 連續幾次「找不到」就自動移出追蹤名單。
#
# 一次不夠 —— 手滑打錯 id、平台暫時性故障
# 都會給出一次 404。先寫成模組常數，真的需要調整時再拉到 config。
NOT_FOUND_UNTRACK_AT = 2

# **只有 pixiv**。Fediverse 的 404 最常見原因是改名，而改名有 `sn:` 哨符
# 治療那條路（services/identity.py），自動退訂會打斷它。
AUTO_UNTRACK_PLATFORMS = frozenset({"pixiv"})

# 這些結果**不動** streak：它們都不是「這個帳號不存在」的證據。
#   skipped        —— 那一輪根本沒查（站台被限速）
#   rate_limited   —— 對方在擋，與帳號存不存在無關
#   auth_required  —— 我們的憑證問題
#   failed         —— 包含「平台改版」，正是不可以拿來退訂的那一種
# 也不歸零：一次限速不該把前面兩次真的找不到洗掉。
_STREAK_NEUTRAL = frozenset({
    FetchStatus.SKIPPED.value,
    FetchStatus.RATE_LIMITED.value,
    FetchStatus.AUTH_REQUIRED.value,
    FetchStatus.FAILED.value,
})


def _apply_not_found_streak(
    acc: Account, status: str, job: Job, now: datetime
) -> bool:
    """更新連續「找不到」計數，必要時自動退訂。回傳「這一輪退訂了嗎」。

    抽成模組層函式而不是寫在 `_record()` 裡面，是為了能單獨測 ——
    這段邏輯的每一條分支都是「什麼情況下**不該**退訂」，
    而那正是最需要被釘住、也最容易在日後被順手放寬的東西。
    """
    if status in _STREAK_NEUTRAL:
        return False

    if status != FetchStatus.NOT_FOUND.value:
        # ok / no_new = 這個帳號活著。連續中斷，歸零。
        acc.not_found_streak = 0
        return False

    # ⚠️ 泛用的 HTTP 404 **不算數**。只有 recon 驗證過的形狀才累積
    # （見 `PixivNotFound`）—— 否則 pixiv 改版當天會把整批帳號退訂掉。
    if not job.not_found_confirmed:
        return False

    acc.not_found_streak = (acc.not_found_streak or 0) + 1
    if (acc.platform not in AUTO_UNTRACK_PLATFORMS
            or acc.not_found_streak < NOT_FOUND_UNTRACK_AT
            or not acc.is_tracked):
        return False

    acc.is_tracked = False
    acc.last_fetch_note = (
        f"連續 {acc.not_found_streak} 次找不到（{now:%Y-%m-%d}），已自動移出追蹤名單"
    )
    log.warning(
        "自動移出追蹤名單：%s（連續 %d 次找不到）",
        account_label(acc), acc.not_found_streak,
    )
    return True


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
        # ⚠️ **排在 `is_tracked` 之前判斷。** 一個既被忽略又沒在追蹤的帳號，
        # 講「你標記為忽略」比講「已取消追蹤」有用 —— 那是使用者剛做的事，
        # 而且是他自己能改回來的那一個。順序顛倒的話，他會去查一個
        # 其實與他無關的自動退訂。
        if acc.is_ignored:
            plan.skip("ignored", label)
        elif not acc.is_tracked:
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
            # ⚠️ **哨符不可以拿去查平台。** `sn:<name>` 的意思是「還沒有真正的
            # 平台 id」，把它塞進 `users/show {"userId": ...}` 只會拿到 400。
            # 實測（正式庫，2026-08-15）：9 個 misskey 帳號裡有真 id 的那個成功、
            # 8 個哨符全部 400 —— 當時被誤判成限速。
            # 傳 None 就會改走名字解析，而查到的真 id 會由
            # `identity.heal_placeholder_account` 就地寫回同一列。
            user_id = None if is_placeholder(acc.platform_user_id) else acc.platform_user_id
            plan.targets.append((target, user_id))
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
    # 這一輪的結果分類（`FetchStatus` 的值）。**`_process()` 本來就算出來了**，
    # 只是以前沒往外送 —— 前端因此只看得到一個紅色的 failed，要分類就只能去
    # 比對錯誤字串裡有沒有「429」，而那是平台文案一改就失效的耦合。
    #
    # ⚠️ 重試功能的**所有**分類都以這個欄位為準，不准再解析 `error`。
    fetch_status: str | None = None
    # 第幾次嘗試。前端把同一個帳號的多次嘗試摺疊成一列，徽章顯示它。
    attempt: int = 1
    # 上一次嘗試的 job id，串起歷次（展開徽章時要逐次列出原因）
    retry_of: int | None = None
    # 續抓：從 `accounts.resume_cursor` 接下去，不是從第 1 頁重來。
    # ⚠️ 與 full 不同 —— full 是「從頭重掃且不提早停」。
    resume: bool = False
    # 這一輪的「找不到」是 **recon 驗證過的形狀**嗎？只有它才累積退訂計數。
    # 泛用的 HTTP 404（可能只是端點沒了、或 Fediverse 改名）不算 ——
    # 見 `PixivNotFound` 的說明。
    not_found_confirmed: bool = False

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
            "fetch_status": self.fetch_status,
            "attempt": self.attempt,
            "retry_of": self.retry_of,
            "resume": self.resume,
            # 前端摺疊要用的分組鍵。讓後端給，而不是前端自己拼 ——
            # 拼法不一致的話，同一個帳號會被拆成兩列（或兩個帳號被併成一列）。
            "key": "|".join(self.key),
            "retryable": self.retryable,
            "resumable": self.resumable,
        }

    # ── 重試分類 ─────────────────────────────────────
    #
    # 「全部重試」只吃這三類。另外兩類（缺憑證、找不到）**不是**做成
    # disabled 按鈕，而是文案不同的「仍要重試」——
    # 使用者剛填完憑證、剛確認過改名時，他知道的比系統多。

    @property
    def retryable(self) -> bool:
        """值得放進「全部重試」嗎。

        `SKIPPED` 是最主要的服務對象：站台被限速那一輪它**根本沒跑過**。
        """
        return self.fetch_status in {
            FetchStatus.SKIPPED.value,
            FetchStatus.RATE_LIMITED.value,
            FetchStatus.FAILED.value,
        }

    @property
    def resumable(self) -> bool:
        """撞到頁數上限、可以「繼續抓」嗎。

        ⚠️ 這**不是失敗**（state 是 done）。與重試分開成兩個動作、兩套文案 ——
        混在一起會讓失敗分類的明細說謊。

        id 清單式平台（pixiv）走 `_fetch_by_id_list`，**沒有頁數上限的概念**，
        所以永遠不會 resumable。
        """
        if self.state != "done" or not self.result:
            return False
        return "頁數上限" in str(self.result.get("stopped_because") or "")


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
    # 本輪自動退訂的帳號標籤。與 `_history` 同樣是記憶體狀態（重啟就沒了），
    # 但退訂本身寫在 DB 裡，這裡只是給摘要用的「剛剛發生了什麼」。
    _auto_untracked: list[str] = field(default_factory=list, init=False)
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
        attempt: int = 1,
        retry_of: int | None = None,
        resume: bool = False,
    ) -> Job | None:
        """排一個帳號。已經在跑或已在佇列裡就回 None（不重複排）。"""
        key = (target.platform, target.host, target.acct.lower())
        if key in self._active:
            return None

        # 佇列本來是空的 = 這是新的一批。退訂清單只講「這一批」發生了什麼，
        # 不清掉的話，昨天的退訂會一直掛在今天的摘要上。
        # ⚠️ 清在這裡而不是 `_drain()`：介面是在跑完之後才去讀的，
        # 在 drain 清掉等於使用者永遠看不到。
        if not self._pending and self._running is None:
            self._auto_untracked.clear()

        job = Job(
            id=self._next_id,
            platform=target.platform,
            host=target.host,
            acct=target.acct,
            full=full,
            user_id=user_id,
            attempt=attempt,
            retry_of=retry_of,
            resume=resume,
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
            # 這一批裡被自動移出追蹤名單的帳號。**一定要回**：
            # 靜默退訂就算技術上正確，使用者也只會覺得帳號自己不見了。
            "auto_untracked": list(self._auto_untracked),
            # 重啟就沒了，介面要講明
            "volatile": True,
            # ⚠️ 歷史滿了 = 更早的結果**已經沒了**，「全部重試」做不到真的全部。
            # 一鍵更新是 442 個帳號而上限是 200，所以這是預設情況不是邊緣情況。
            # 介面要照實講，不可以讓數字自己對不上。
            "history_limit": _HISTORY_LIMIT,
            "history_full": len(history) >= _HISTORY_LIMIT,
        }

    def start(self) -> None:
        """在有 event loop 的地方先把 worker 起來（lifespan 會呼叫）。

        ⚠️ `enqueue()` 必須在 event loop 上呼叫。FastAPI 會把 **sync def**
        的端點丟到 threadpool，那裡沒有 running loop，`asyncio.Event.set()`
        也不是 thread-safe —— 所以排入佇列的端點一律寫成 `async def`。
        """
        self._ensure_worker()

    # ── 重試 ─────────────────────────────────────────────

    def _requeue(self, job: Job, *, resume: bool = False) -> Job | None:
        """把一筆歷史工作重新排進去。回 None = 該帳號已經在佇列裡。"""
        return self.enqueue(
            Target(platform=job.platform, host=job.host, acct=job.acct),
            full=job.full,
            user_id=job.user_id,
            attempt=job.attempt + 1,
            retry_of=job.id,
            resume=resume,
        )

    def _find(self, job_id: int) -> Job | None:
        for job in self._history:
            if job.id == job_id:
                return job
        return None

    def retry(self, job_id: int, *, resume: bool = False) -> dict[str, Any]:
        """重試（或續抓）單一筆。

        單筆是**使用者覆寫**：不可重試的類別（缺憑證、找不到）也允許，
        因為使用者剛去設定頁填完憑證、剛在帳號頁確認過改名時，
        他知道的比系統多。擋掉他等於擋掉唯一合理的使用情境。
        """
        job = self._find(job_id)
        if job is None:
            # 歷史只留 `_HISTORY_LIMIT` 筆，滾掉了就真的沒了。
            # 出聲，不要靜默失敗 —— 使用者會以為按了有效。
            return {"requeued": False, "job": None,
                    "refused_reason": "這筆已經不在佇列歷史裡（只留最後"
                                      f" {_HISTORY_LIMIT} 筆）"}
        if resume and not job.resumable:
            return {"requeued": False, "job": job.as_dict(),
                    "refused_reason": "這筆沒有撞到頁數上限，不需要續抓"}
        fresh = self._requeue(job, resume=resume)
        if fresh is None:
            return {"requeued": False, "job": job.as_dict(),
                    "refused_reason": f"{job.label} 已經在佇列裡了"}
        return {"requeued": True, "job": fresh.as_dict(), "refused_reason": None}

    def retry_failed(
        self,
        *,
        include_skipped: bool = True,
        include_unretryable: bool = False,
        clear_rate_limit: bool = False,
    ) -> dict[str, Any]:
        """把歷史裡可重試的全部重排。

        ⚠️ **先清旗標再排。** 反過來的話，中間那段時間窗會讓前幾筆在
        `_process()` 開頭就被標成 skipped —— 使用者會看到「已排入 201 個」
        然後幾秒內全部變 ⊘，而完全不知道發生了什麼。
        """
        if clear_rate_limit:
            self.clear_rate_limit()

        # 同一個帳號在歷史裡可能有好幾筆（重試過幾次）。只重排**最新那一筆**，
        # 否則一個帳號會被排三次 —— 而 `_active` 只擋得住其中兩次，
        # 剩下那次的 attempt 還會是舊的。
        latest: dict[tuple[str, str, str], Job] = {}
        for job in self._history:
            latest[job.key] = job

        candidates: list[Job] = []
        for job in latest.values():
            if job.retryable:
                if job.fetch_status == FetchStatus.SKIPPED.value and not include_skipped:
                    continue
                candidates.append(job)
            elif include_unretryable and job.state == "failed":
                candidates.append(job)

        queued: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        will_be_skipped = 0
        for job in candidates:
            fresh = self._requeue(job)
            if fresh is None:
                refused.append({"job_id": job.id, "label": job.label,
                                "reason": "已經在佇列裡"})
                continue
            queued.append(fresh.as_dict())
            # 旗標還掛著的站台，這一筆進去也是直接被跳過。**事前講**，
            # 不要讓使用者事後自己發現。
            if (fresh.platform, fresh.host) in self._rate_limited:
                will_be_skipped += 1

        return {
            "requeued": len(queued),
            "jobs": queued,
            "refused": refused,
            "will_be_skipped": will_be_skipped,
        }

    def resume_capped(self) -> dict[str, Any]:
        """把撞到頁數上限的全部續抓。與 `retry_failed()` 是**兩個**動作。"""
        latest: dict[tuple[str, str, str], Job] = {}
        for job in self._history:
            latest[job.key] = job

        queued: list[dict[str, Any]] = []
        refused: list[dict[str, Any]] = []
        for job in latest.values():
            if not job.resumable:
                continue
            fresh = self._requeue(job, resume=True)
            if fresh is None:
                refused.append({"job_id": job.id, "label": job.label,
                                "reason": "已經在佇列裡"})
            else:
                queued.append(fresh.as_dict())
        return {"requeued": len(queued), "jobs": queued, "refused": refused}

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
            await self._pace()
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

            if self._pending:
                await self._pace()
            else:
                await self._drain()

    async def _pace(self) -> None:
        """兩個帳號之間喘一口氣。

        佇列本來就是序列的，但帳號與帳號之間**完全沒有間隔** —— 實測一批
        misskey 帳號是 200 ms 一個。列舉的節流（`fetch_delay_seconds`）
        只管同一個帳號的翻頁，管不到這裡。

        ⚠️ **這不是那批 HTTP 400 的解方。** 那是拿 `sn:` 哨符當 userId 去查
        造成的（見 `services/identity.py`），加多少延遲都不會變好。
        這個延遲純粹是對站台的禮貌 —— 別讓「一鍵更新 442 個帳號」變成
        對同一台伺服器的連續叩門。
        """
        delay = self.cfg.fetch_account_delay_seconds
        if delay > 0:
            await asyncio.sleep(delay)

    async def _process(self, job: Job) -> None:
        limited = self._rate_limited.get((job.platform, job.host))
        if limited:
            job.state = "skipped"
            job.reason = limited
            # ⚠️ 這條路徑提早 return，很容易在加欄位時被漏掉 ——
            # 而 skipped 正是**最該被重試**的一類（它根本沒跑過）。
            job.fetch_status = FetchStatus.SKIPPED.value
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
                resume=job.resume,
            )
            job.state = "done"
            job.result = result.as_dict()
            # 解析出來的真 id 記回 job：`_record()` 要靠它找到 DB 那一列，
            # 而批次抓新加的帳號一開始是沒有 user_id 的。
            job.user_id = job.user_id or result.platform_user_id or None
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
                # 平台在 body 裡講了為什麼，丟掉它等於逼使用者去猜。
                # 這次就猜錯了：8 個 misskey 帳號回 400，被讀成「掃太快」，
                # 實際上是拿 `sn:` 哨符當 userId 去查（見 services/identity.py）。
                why = _error_detail(exc.response)
                job.error = f"HTTP {code}：{job.label}" + (f" —— {why}" if why else "")
                outcome = (FetchStatus.FAILED.value, job.error, None)
        except PixivNotFound as exc:
            # ⚠️ 必須排在 `except Exception` 之前，否則永遠走不到這裡。
            # 與上面那個泛用 404 分支的差別：這一個是 recon 驗證過的形狀
            # （404 + JSON + error:true），**只有它才算數**去累積退訂計數。
            job.state = "failed"
            job.error = str(exc)
            job.not_found_confirmed = True
            outcome = (FetchStatus.NOT_FOUND.value, job.error, None)
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

        # 分類要送到前端。以前只寫進 DB 的 accounts.last_fetch_status，
        # 佇列這邊丟掉了 —— 於是畫面上五種結局全部壓成一個紅色的 failed。
        job.fetch_status = outcome[0]

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
                base = select(Account).where(
                    Account.platform == job.platform,
                    Account.instance_host == job.host,
                )
                # 有 user_id 就**只**用它。帳號會改名，這是唯一穩定的鍵；
                # 而且明確給了 id 卻改用名字比對，正是會把結果寫到別人那一列
                # 的猜測（`sn:` 哨符那批就是這樣分裂的）。找不到要出聲。
                if job.user_id:
                    candidates = [Account.platform_user_id == job.user_id]
                else:
                    # 沒有 id 的情況：抓取在解析出 id 之前就失敗了。
                    # 依序試兩種「acct 是什麼」的可能，不用 OR ——
                    # OR 可能同時命中兩列，那時挑哪一列是碰運氣。
                    #   1. acct 是 handle：游標式平台（Fediverse）
                    #   2. acct 是數字 id：**id 清單式平台（pixiv）**。
                    #      它的 screen_name 是暱稱，拿 acct 去比對永遠不會中。
                    candidates = [
                        Account.screen_name == job.acct,
                        Account.platform_user_id == job.acct,
                    ]

                acc = None
                for cond in candidates:
                    acc = session.scalars(base.where(cond)).first()
                    if acc is not None:
                        break
                if acc is None:
                    # 出聲，不要靜默跳過。找不到通常代表 ingest 把它建成了
                    # 另一列（例如 Fediverse 的 handle 被解析成真實數字 id，
                    # 而 DB 裡原本是 `sn:` 暫代的那一列）—— 那是真的問題。
                    log.warning(
                        "記錄擷取結果時找不到帳號：%s（user_id=%s）",
                        job.label, job.user_id,
                    )
                    return
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                acc.last_fetched_at = now
                acc.last_fetch_status = status
                acc.last_fetch_note = (note or None)
                acc.last_fetch_new_posts = new_posts
                if _apply_not_found_streak(acc, status, job, now):
                    # 統計給一鍵更新的摘要用。靜默退訂就算技術上正確，
                    # 使用者也只會覺得帳號自己不見了。
                    self._auto_untracked.append(account_label(acc))
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
