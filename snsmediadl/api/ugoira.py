"""pixiv 動圖（ugoira）的逐格供應。

ugoira 下載下來是一包 zip 裝一堆 jpg，瀏覽器沒有任何原生元素吃得下它。
這支把它拆成「幀表 + 逐格圖片」兩個端點，前端拿 canvas 自己播
（見 `web/js/ugoira.js`）。**不轉檔、不依賴 ffmpeg。**

### 為什麼逐格供應是便宜的

pixiv 的 ugoira zip 是 **STORED（完全沒壓縮）**——2026-08-22 對實檔驗證：
96 格 800×566、8.6 MB、`compress_type == 0`。所以「取出第 n 格」是從檔案
某個 offset 讀一段連續 bytes，不必 inflate、不必解壓到暫存目錄。
每次請求重開一次 zip 只是重讀中央目錄（96 筆約 5 KB），**刻意不做快取**：
省下的時間遠小於一個快取失效 bug 的代價。

### 幀延遲只有 DB 有

zip 裡**沒有**任何時間資訊，每格延遲來自擷取當下存下的
`media.meta_json["frames"]`（`adapters/pixiv.py` 的 `_ugoira_media`）。
沒有這份資料就播不出正確的動畫——那時候要明說，
**不可以猜一個 fps 頂替**：猜出來的動畫會用錯的速度播放，而且畫面上
沒有任何跡象告訴使用者它是錯的。
"""

from __future__ import annotations

import json
import mimetypes
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..config import Config
from ..db.enums import MediaKind
from ..db.models import Media
from .app import get_config, get_session
from .errors import ApiError
from .files import FILE_METHODS, IMMUTABLE, resolve_media_file

router = APIRouter(prefix="/api", tags=["ugoira"])


def _ugoira_row(media_id: int, session: Session) -> Media:
    """這筆是不是動圖？

    ⚠️ **必須在碰磁碟之前問。** 反過來的話，對一筆 photo 問動圖資料會先撞上
    「原檔不在了」——那是**捏造診斷**：檔案在不在跟「這個請求根本不合理」
    是兩件事，而使用者會照著錯的那句去查。2026-08-22 實測踩到。
    """
    media = session.get(Media, media_id)
    if media is None:
        raise ApiError("media.not_found", "No such media.", 404)
    if media.kind != MediaKind.UGOIRA.value:
        # 415 而不是 404：這筆存在，只是它不是動圖。
        raise ApiError(
            "ugoira.not_ugoira",
            f"Media {media_id} is {media.kind!r}, not an ugoira.",
            415,
        )
    return media


def _frames(media: Media) -> tuple[list[dict], str | None]:
    """讀出這筆媒體的幀表。拿不到就丟出說得清楚的錯誤。

    ⚠️ **這裡的每一個 raise 都不可以改成回一份預設值。** 幀表壞掉或不存在
    代表「這筆沒辦法誠實播放」，而不是「用 25 fps 播播看」。
    """
    media_id = media.id
    meta = json.loads(media.meta_json) if media.meta_json else {}
    frames = meta.get("frames")
    if not frames:
        # 匯入進來的、或未來其他來源的 ugoira 可能沒有幀資料。修法是重抓
        # `ugoira_meta` 把它補上，不是在這裡編一個延遲出來。
        raise ApiError(
            "ugoira.no_frame_data",
            f"Media {media_id} has no ugoira frame data; re-fetch it from pixiv "
            "to record the per-frame delays.",
            409,
        )
    if not isinstance(frames, list) or not all(
        isinstance(f, dict) and isinstance(f.get("file"), str)
        and isinstance(f.get("delay"), int)
        for f in frames
    ):
        raise ApiError(
            "ugoira.bad_frame_data",
            f"Media {media_id} has ugoira frame data in an unexpected shape.",
            500,
        )
    return frames, meta.get("mime_type")


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ApiError(
            "ugoira.unreadable", f"The ugoira zip cannot be opened: {exc}", 500,
        ) from exc


@router.api_route("/media/{media_id}/ugoira", methods=FILE_METHODS)
def get_ugoira_meta(
    media_id: int,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> dict:
    """幀表：這包動圖有幾格、每格停多久。

    ⚠️ **會拿 zip 的實際名單交叉驗證，而且不一致就報錯、不取交集。**
    取交集的話，pixiv 改了打包方式時畫面上只會少幾格——沒有錯誤訊息，
    只是動畫變得怪怪的，而那是幾個月後才會有人發現的那種 bug。
    """
    media = _ugoira_row(media_id, session)
    path = resolve_media_file(media_id, session, cfg)
    frames, mime_type = _frames(media)

    with _open_zip(path) as zf:
        in_zip = sorted(n for n in zf.namelist() if not n.endswith("/"))
    listed = sorted(f["file"] for f in frames)
    if in_zip != listed:
        missing = [n for n in listed if n not in in_zip]
        extra = [n for n in in_zip if n not in listed]
        raise ApiError(
            "ugoira.frame_mismatch",
            f"The frame list and the zip disagree for media {media_id}: "
            f"{len(missing)} listed frame(s) missing from the zip, "
            f"{len(extra)} extra entr(ies) in the zip.",
            500,
        )

    return {
        "count": len(frames),
        "total_ms": sum(f["delay"] for f in frames),
        "mime_type": mime_type,
        # 只給延遲，不給檔名——前端用索引定址，知道檔名對它沒有用處，
        # 而少一個欄位就少一處會跟 pixiv 的打包方式綁在一起。
        "frames": [{"delay": f["delay"]} for f in frames],
    }


@router.api_route("/media/{media_id}/ugoira/{index}", methods=FILE_METHODS)
def get_ugoira_frame(
    media_id: int,
    index: int,
    request: Request,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> Response:
    """第 `index` 格的原始 bytes。原檔直出，不重新編碼。"""
    media = _ugoira_row(media_id, session)
    path = resolve_media_file(media_id, session, cfg)
    frames, mime_type = _frames(media)

    if not 0 <= index < len(frames):
        raise ApiError(
            "ugoira.frame_out_of_range",
            f"Frame {index} is out of range; media {media_id} has {len(frames)}.",
            404,
        )

    name = frames[index]["file"]
    with _open_zip(path) as zf:
        try:
            info = zf.getinfo(name)
        except KeyError as exc:
            # 單格對不上也是幀表與 zip 不一致，跟幀表端點回同一個 code。
            raise ApiError(
                "ugoira.frame_mismatch",
                f"Frame {index} ({name!r}) is not in the zip for media {media_id}.",
                500,
            ) from exc
        # 媒體檔的內容永遠不會變，快取策略沿用 `files.py` 的同一個常數。
        headers = {"Cache-Control": IMMUTABLE, "Content-Length": str(info.file_size)}
        mime = mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        # HEAD 不讀 bytes——只是問「這格在不在、多大」，讀出來只是浪費。
        if request.method == "HEAD":
            return Response(status_code=200, media_type=mime, headers=headers)
        return Response(content=zf.read(info), media_type=mime, headers=headers)
