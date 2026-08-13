"""佇列執行的單一入口。

**為什麼需要這層**：`run_worker` 本身沒有任何互斥保護，而
`_load_pending` 撈的是 `status == PENDING`，要進到 `_download_one` 才會標
`DOWNLOADING`。也就是說兩個 worker 併行時，兩邊都會撿到同一批 ——
同一個檔會被下載兩次，`resolve_collision` 還會很配合地產生 `xxx (1).jpg`。

背景迴圈與 `POST /api/queue/run` 都走這裡，就不可能併行。
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session, sessionmaker

from ..config import Config
from .worker import WorkerStats, run_worker


class QueueRunner:
    """一次只跑一輪。已經在跑就立刻回 None，**不排隊等待**。

    ⚠️ 為什麼是一個 bool 而不是 `asyncio.Lock`：
    `asyncio.Lock` 在 3.10+ 會在第一次使用時綁定當時的 event loop，之後從別的
    loop 用就丟 RuntimeError。這個 runner 是模組層單例，而每個 pytest 測試都
    有自己的 loop —— 用 Lock 會在第二個測試就炸掉。

    bool 在這裡是安全的：asyncio 是單執行緒，而「檢查」與「設旗標」之間
    沒有任何 await，其他 coroutine 插不進來。
    """

    def __init__(self) -> None:
        self._running = False
        self._last_stats: WorkerStats | None = None
        self._last_finished_at: str | None = None

    def is_running(self) -> bool:
        return self._running

    def last_run(self) -> dict | None:
        """上一輪的結果。`None` 代表這個 process 還沒跑過任何一輪 ——
        與「跑過但什麼都沒做」是不同的狀態，不可以合併成同一個回答。"""
        if self._last_stats is None:
            return None
        return {**self._last_stats.as_dict(), "finished_at": self._last_finished_at}

    def reset(self) -> None:
        """測試用。單例跨測試共用，不清就會帶著上一個測試的結果。"""
        self._running = False
        self._last_stats = None
        self._last_finished_at = None

    async def run_once(
        self,
        cfg: Config,
        maker: sessionmaker[Session],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limit: int | None = None,
    ) -> WorkerStats | None:
        if self._running:
            return None
        self._running = True
        try:
            stats = await run_worker(cfg, maker, transport=transport, limit=limit)
            self._last_stats = stats
            self._last_finished_at = datetime.now(timezone.utc).isoformat()
            return stats
        finally:
            self._running = False


# 模組層單例。背景迴圈與 API 端點必須是同一個，否則互斥就不存在。
runner = QueueRunner()
