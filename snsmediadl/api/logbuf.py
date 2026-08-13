"""記憶體 ring buffer 日誌。

GUI 要能看到伺服器發生什麼事 —— 沒有這個的話，下載失敗只會表現成
「數字對不上」，使用者不知道去哪查。刻意不落地：這是即時診斷用的，
不是稽核紀錄。
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone

MAX_RECORDS = 500

_buffer: deque[dict] = deque(maxlen=MAX_RECORDS)


class RingBufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _buffer.append(
                {
                    "ts": datetime.fromtimestamp(
                        record.created, tz=timezone.utc
                    ).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "exc": self.format(record) if record.exc_info else None,
                }
            )
        except Exception:  # pragma: no cover - logging 不能反過來炸掉程式
            pass


def install(level: int = logging.INFO) -> None:
    handler = RingBufferHandler()
    handler.setLevel(level)
    root = logging.getLogger()
    if not any(isinstance(h, RingBufferHandler) for h in root.handlers):
        root.addHandler(handler)
    if root.level > level:
        root.setLevel(level)


def records(limit: int = 200, level: str | None = None) -> list[dict]:
    items = list(_buffer)
    if level:
        wanted = level.upper()
        items = [r for r in items if r["level"] == wanted]
    return items[-limit:][::-1]  # 新的在前


def clear() -> None:
    _buffer.clear()
