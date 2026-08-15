"""把下載好的檔案提供給瀏覽器顯示。

這個端點會把磁碟上的檔案吐出去，所以路徑檢查不是選配。
`local_path` 目前是自己寫進 DB 的，但未來的匯入功能、手動改 DB、
或任何寫入路徑的 bug，都會讓它變成任意檔案讀取的入口。
"""

from __future__ import annotations

import logging
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

log = logging.getLogger("snsmediadl")


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


# 媒體檔的內容永遠不會變（改了就是另一個檔），所以可以讓瀏覽器無限期快取。
# 少了這個 header，捲回上一頁就是整頁重傳。
IMMUTABLE = "public, max-age=31536000, immutable"

# 縮圖規格。320px 是格線一格的兩倍（供 HiDPI），再大就只是浪費頻寬。
THUMB_MAX_EDGE = 320
THUMB_QUALITY = 80

# 能生縮圖的副檔名。**白名單而不是黑名單** —— 遇到沒看過的格式要明確回
# 「不支援」，不要丟進 Pillow 賭賭看。
THUMBABLE_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
})


def _resolve_media_file(media_id: int, session: Session, cfg: Config) -> Path:
    """共用的「查記錄 → 檢查路徑 → 確認檔案在」流程。"""
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
    return path


@router.get("/media/{media_id}/file")
def get_media_file(
    media_id: int,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> FileResponse:
    path = _resolve_media_file(media_id, session, cfg)
    mime, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path,
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": IMMUTABLE},
    )


def _thumb_cache_path(cfg: Config, media_id: int) -> Path:
    """快取檔位置。分 256 個桶 —— 224 萬個檔案塞同一個目錄，光是 `ls` 就會卡住。"""
    return cfg.thumb_dir / f"{media_id % 256:02x}" / f"{media_id}.webp"


def _render_thumb(src: Path, dst: Path) -> None:
    """生一張縮圖到 `dst`。失敗就讓例外往上拋。

    ⚠️ **不 fallback 成佔位圖。** 生不出來多半代表原檔壞了或格式沒支援，
    回一張灰方塊等於把壞檔藏起來 —— 使用者會以為那張圖本來就長那樣，
    然後在幾個月後才發現一批檔案是壞的。
    """
    from PIL import Image, ImageOps

    with Image.open(src) as im:
        # 手機拍的照片帶 EXIF 方向旗標。不轉的話縮圖是躺著的，
        # 而原圖在檢視器裡卻是正的 —— 看起來像縮圖抓錯檔案。
        im = ImageOps.exif_transpose(im)
        im.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.LANCZOS)
        if im.mode not in ("RGB", "RGBA"):
            # P（調色盤）與 LA 直接存 WebP 會失敗或掉色
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        dst.parent.mkdir(parents=True, exist_ok=True)
        # 先寫暫存再 rename：中途被中斷的話，留下的是半張圖，而它會被
        # 當成有效快取一直吐出去。rename 在同一個檔案系統上是原子的。
        tmp = dst.with_suffix(".webp.part")
        im.save(tmp, "WEBP", quality=THUMB_QUALITY, method=4)
        tmp.replace(dst)


@router.get("/media/{media_id}/thumb")
def get_media_thumb(
    media_id: int,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> FileResponse:
    """格線用的縮圖。320px WebP，生成後存到磁碟快取。

    ### 為什麼需要這支

    格線原本直接吐原檔。正式庫的實際數字：224 萬個媒體、總計 1.27 TB、
    **單檔最大 446 MB**。一頁 60 格就是上百 MB 的跨磁碟 I/O ——
    而顯示出來只有 160px 見方。

    ### 按需生成

    第一次要求時才生，生完存著。**不做批次預生成** —— 那要遍歷三顆碟上的
    224 萬個檔案，成本遠超過收益，而且那是使用者的媒體庫。

    ### 影片不生縮圖

    抽影格要 ffmpeg（外部執行檔依賴）。影片回 415，前端顯示佔位不掛
    `<video>` 元素 —— 原本每格一個 `preload="metadata"` 等於 60 次開檔讀 moov。
    """
    path = _resolve_media_file(media_id, session, cfg)

    if path.suffix.lower() not in THUMBABLE_SUFFIXES:
        # 415 而不是 404：檔案在，只是這種格式做不出縮圖。
        # 兩者混用的話，前端分不出「檔案不見了」與「這是影片」。
        raise HTTPException(415, f"不支援為 {path.suffix} 生縮圖")

    cache = _thumb_cache_path(cfg, media_id)
    if not cache.exists():
        try:
            _render_thumb(path, cache)
        except Exception as exc:  # noqa: BLE001
            log.warning("縮圖生成失敗 media#%s（%s）：%s", media_id, path, exc)
            raise HTTPException(500, f"縮圖生成失敗：{exc}") from exc

    return FileResponse(
        cache, media_type="image/webp", headers={"Cache-Control": IMMUTABLE}
    )
