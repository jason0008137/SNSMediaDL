"""把下載好的檔案提供給瀏覽器顯示。

這個端點會把磁碟上的檔案吐出去，所以路徑檢查不是選配。
`local_path` 目前是自己寫進 DB 的，但未來的匯入功能、手動改 DB、
或任何寫入路徑的 bug，都會讓它變成任意檔案讀取的入口。
"""

from __future__ import annotations

import mimetypes
from collections.abc import Iterable
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import Config
from ..db.models import Media
from .app import get_config, get_session

router = APIRouter(prefix="/api", tags=["files"])


def resolve_safe_path(local_path: str, roots: Iterable[Path]) -> Path:
    """確認路徑真的落在其中一個允許的根目錄底下。都不在就拒絕。

    多個根目錄的理由見 `Config.extra_media_roots`：換過下載目錄之後，舊檔
    仍要看得到。白名單比對的性質沒變，只是白名單有多筆 —— 任一命中即通過。
    """
    target = Path(local_path).resolve()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            # 根目錄所在的磁碟沒插是常態，跳過就好，不能讓其他根目錄跟著壞掉
            continue
        # is_relative_to 是 3.9+；不用字串前綴比對，那個會被 /out-evil 這種騙過
        if target.is_relative_to(resolved):
            return target
    raise HTTPException(403, "檔案不在允許的媒體目錄內，拒絕提供")


@router.get("/media/{media_id}/file")
def get_media_file(
    media_id: int,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> FileResponse:
    media = session.get(Media, media_id)
    if media is None:
        raise HTTPException(404, "media not found")
    if not media.local_path:
        raise HTTPException(409, "尚未下載")

    path = resolve_safe_path(media.local_path, cfg.media_roots)
    if not path.exists():
        # 檔案被手動刪掉是常見情況 —— 回 404 讓 GUI 標示「檔案遺失」，
        # 不要讓整頁壞掉。
        raise HTTPException(404, "檔案遺失")

    mime, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=mime or "application/octet-stream")
