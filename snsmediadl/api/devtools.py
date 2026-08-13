"""開發用：extension 的診斷回報與自動重載。

存在的理由：extension 跑在瀏覽器裡，開發時看不到它的錯誤與狀態 ——
只能靠使用者用文字轉述症狀，那是很差的回饋迴圈。
這兩個端點把 extension 的「黑盒子」接出來。
"""

from __future__ import annotations

import hashlib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..config import Config
from .app import get_config

router = APIRouter(prefix="/api", tags=["devtools"])

MAX_ENTRIES = 400
_entries: deque[dict] = deque(maxlen=MAX_ENTRIES)

EXTENSION_DIR = Path(__file__).resolve().parent.parent.parent / "extension"
_WATCHED = (".js", ".json", ".html", ".css")


class ExtLogEntry(BaseModel):
    level: str = "info"
    event: str
    detail: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    where: str | None = None          # bar / content / background / popup


class ExtLogBatch(BaseModel):
    entries: list[ExtLogEntry]


@router.post("/ext-log")
def post_ext_log(batch: ExtLogBatch) -> dict:
    """extension 回報自己的狀態與錯誤。"""
    now = datetime.now(timezone.utc).isoformat()
    for e in batch.entries:
        _entries.append({
            "ts": now,
            "level": e.level,
            "where": e.where,
            "event": e.event,
            "detail": e.detail,
            "context": e.context,
        })
    return {"received": len(batch.entries)}


@router.get("/ext-log")
def get_ext_log(limit: int = 100, level: str | None = None) -> dict:
    items = list(_entries)
    if level:
        wanted = level.lower()
        items = [i for i in items if i["level"].lower() == wanted]
    return {"items": items[-limit:][::-1], "total": len(_entries)}


@router.delete("/ext-log")
def clear_ext_log() -> dict:
    n = len(_entries)
    _entries.clear()
    return {"cleared": n}


def extension_fingerprint() -> str:
    """extension 目錄的內容指紋。

    用 mtime + 大小而不是讀檔內容 —— 這個端點會被輪詢，不該每次都讀完整個目錄。
    """
    if not EXTENSION_DIR.is_dir():
        return "no-extension-dir"
    h = hashlib.sha256()
    for path in sorted(EXTENSION_DIR.rglob("*")):
        if path.is_file() and path.suffix in _WATCHED and not path.name.startswith("test_"):
            st = path.stat()
            h.update(f"{path.name}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()[:16]


@router.get("/ext-version")
def get_ext_version(cfg: Config = Depends(get_config)) -> dict:
    """extension 檔案的指紋。變了就代表該重載。

    這樣我改完檔案不需要任何額外動作 —— 指紋自己會變，extension 自己會重載。
    """
    return {"fingerprint": extension_fingerprint(), "dev_reload": cfg.dev_reload}
