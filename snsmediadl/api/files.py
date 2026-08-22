"""把下載好的檔案提供給瀏覽器顯示。

這個端點會把磁碟上的檔案吐出去，所以路徑檢查不是選配。
`local_path` 目前是自己寫進 DB 的，但未來的匯入功能、手動改 DB、
或任何寫入路徑的 bug，都會讓它變成任意檔案讀取的入口。
"""

from __future__ import annotations

import io
import logging
import mimetypes
import subprocess
import threading
import zipfile
from collections.abc import Iterable
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
# ⚠️ Starlette 的那一個。`ApiError` 繼承的是它 —— 抓 FastAPI 的那個子類會
# 漏掉（兩者是兄弟不是父子），症狀是講得清清楚楚的 415 / 503 被蓋成 500。
from starlette.exceptions import HTTPException
from sqlalchemy.orm import Session

from ..config import Config, find_ffmpeg
from ..db.models import Media
from ..fspath import for_io
from .app import get_config, get_session
from .errors import ApiError

router = APIRouter(prefix="/api", tags=["files"])

log = logging.getLogger("snsmediadl")


def resolve_safe_path(local_path: str, roots: Iterable[Path]) -> Path:
    r"""確認路徑真的落在其中一個允許的根目錄底下。都不在就拒絕。

    多個根目錄的理由見 `Config.extra_media_roots`：換過下載目錄之後，舊檔
    仍要看得到。白名單比對的性質沒變，只是白名單有多筆 —— 任一命中即通過。

    ⚠️ **白名單比對用一般路徑，回傳的才是 `\\?\` 形狀**（見 `fspath`）。
    兩者不可以顛倒：`Path(r"\\?\K:\a\b").is_relative_to(Path(r"K:\a"))` 是
    `False`，把前綴加在比對之前，會讓每一個根目錄都對不上，症狀是全庫 403。
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
            # 過了檢查才換成 I/O 形狀 —— 呼叫端拿到的東西一律可以直接碰磁碟，
            # 不必自己記得「這條路徑要不要加前綴」。忘記加就是 606 筆假 404。
            return for_io(target)
    raise ApiError(
        "file.outside_root",
        "This file is outside the allowed media directories.",
        403,
    )


# 媒體檔的內容永遠不會變（改了就是另一個檔），所以可以讓瀏覽器無限期快取。
# 少了這個 header，捲回上一頁就是整頁重傳。
IMMUTABLE = "public, max-age=31536000, immutable"

# 縮圖規格。320px 是格線一格的兩倍（供 HiDPI），再大就只是浪費頻寬。
THUMB_MAX_EDGE = 320
THUMB_QUALITY = 80

# 能生縮圖的副檔名。**白名單而不是黑名單** —— 遇到沒看過的格式要明確回
# 「不支援」，不要丟進 Pillow 賭賭看。
#
# 分三組是因為**三條產生路徑不同**，而且失敗的意義也不同：
#   圖片    Pillow 直接開                 —— 失敗 = 原檔壞了
#   影片    ffmpeg 抽一格再交給 Pillow    —— 失敗可能只是沒裝 ffmpeg（503）
#   ugoira  zip 取第一張再交給 Pillow     —— **不需要 ffmpeg**
THUMBABLE_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff",
})
# X 的 animated_gif 實際存成 .mp4，所以它跟影片是同一條路。
VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".m4v", ".mkv"})
UGOIRA_SUFFIXES = frozenset({".zip"})

# 從第 1 秒抽格。開頭常是黑畫面或淡入，抽第 0 格會得到一片黑 ——
# 那不是「原檔就長這樣」，是抽錯位置。
VIDEO_SEEK_SECONDS = 1
# 單次 ffmpeg 的逾時。壞檔可能讓 ffmpeg 卡住不退出，而它佔著併發閘的名額。
FFMPEG_TIMEOUT = 20
# 排隊等併發閘的上限。等不到就 503，不要讓請求無限期掛著 ——
# 瀏覽器那端會先放棄，而 server 這邊還在跑。
THUMB_QUEUE_TIMEOUT = 10

# ⚠️ 只圈住影片與 ugoira。**圖片不進閘** —— Pillow 開一張 jpg 是毫秒級，
# 圈了只會讓 96% 的請求去排一個它們不需要的隊。
#
# 閘的大小在第一次使用時依 cfg 決定（模組載入時還沒有 cfg）。
_video_gate: threading.BoundedSemaphore | None = None
_gate_lock = threading.Lock()


def _gate(cfg: Config) -> threading.BoundedSemaphore:
    global _video_gate
    with _gate_lock:
        if _video_gate is None:
            _video_gate = threading.BoundedSemaphore(max(1, cfg.thumb_video_concurrency))
        return _video_gate


def reset_thumb_gate() -> None:
    """測試用：換一個併發上限。正式流程不呼叫（改了要重啟）。"""
    global _video_gate
    with _gate_lock:
        _video_gate = None


def resolve_media_file(media_id: int, session: Session, cfg: Config) -> Path:
    """共用的「查記錄 → 檢查路徑 → 確認檔案在」流程。"""
    media = session.get(Media, media_id)
    if media is None:
        raise ApiError("media.not_found", "No such media.", 404)
    if not media.local_path:
        raise ApiError("media.not_downloaded", "This one has not been downloaded yet.", 409)

    path = resolve_safe_path(media.local_path, cfg.media_roots)
    if not path.exists():
        # 檔案被手動刪掉是常見情況 —— 回 404 讓 GUI 標示「檔案遺失」，
        # 不要讓整頁壞掉。
        #
        # ⚠️ `path` 已經是 `\\?\` 形狀（`resolve_safe_path` 保證），所以走到這裡
        # 是**真的**不在了。2026-08-21 之前這裡對 606 筆超過 260 字元的路徑
        # 一律回這個 404，而那些檔案全部好端端在磁碟上 —— 訊息裡的「被刪掉，
        # 或那顆碟沒插」是捏造的診斷。長路徑怎麼處理見 `snsmediadl/fspath.py`。
        raise ApiError(
            "file.missing",
            "The original file is gone (deleted, or that drive is not plugged in).",
            404,
        )
    return path


# ⚠️ **必須明寫 HEAD。** FastAPI 的 `@router.get` 只註冊 GET（與 Starlette 的
# 裸 Route 不同，那個會自動補 HEAD）。少了它，HEAD 會一路掉到掛在 "/" 的
# 靜態檔 mount，回一個 404 —— 而 GUI 正是用 HEAD 去問「讀不到的原因是什麼」：
#   404 = 檔案不在了（被刪，或那顆碟沒插）
#   415 = 這個格式生不出縮圖
#   500 = 原檔壞了
# 沒有 HEAD 的話這三種永遠都會被說成第一種，也就是**捏造診斷**。
FILE_METHODS = ["GET", "HEAD"]


@router.api_route("/media/{media_id}/file", methods=FILE_METHODS)
def get_media_file(
    media_id: int,
    session: Session = Depends(get_session),
    cfg: Config = Depends(get_config),
) -> FileResponse:
    path = resolve_media_file(media_id, session, cfg)
    mime, _ = mimetypes.guess_type(path.name)
    return FileResponse(
        path,
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": IMMUTABLE},
    )


def _thumb_cache_path(cfg: Config, media_id: int) -> Path:
    """快取檔位置。分 256 個桶 —— 224 萬個檔案塞同一個目錄，光是 `ls` 就會卡住。"""
    return cfg.thumb_dir / f"{media_id % 256:02x}" / f"{media_id}.webp"


def _ffmpeg_frame(exe: str, src: Path, seconds: int) -> bytes:
    """抽一格出來，回 PNG bytes。抽不到回空 bytes（不丟例外）。

    `-ss` 放在 `-i` **之前**是關鍵：那是 input seeking，ffmpeg 直接跳到
    關鍵格；放在後面會從頭解碼到那個位置，對一支 446 MB 的片子是好幾秒。
    """
    proc = subprocess.run(
        [exe, "-nostdin", "-loglevel", "error",
         "-ss", str(seconds), "-i", str(src),
         "-frames:v", "1", "-f", "image2", "-vcodec", "png", "-"],
        capture_output=True,
        timeout=FFMPEG_TIMEOUT,
    )
    if proc.returncode != 0 and not proc.stdout:
        log.debug("ffmpeg -ss %s 失敗：%s", seconds, proc.stderr[:200])
    return proc.stdout


def _video_frame_bytes(cfg: Config, src: Path) -> bytes:
    """影片的第一張可用影格。ffmpeg 不可用丟 503，抽不到丟 500。"""
    exe = find_ffmpeg(cfg)
    if exe is None:
        # ⚠️ 503 而不是 415：檔案格式沒問題，是**我們少了依賴**。
        # 混用的話使用者分不出「裝一下 ffmpeg 就好」與「這個檔沒救」。
        raise ApiError(
            "thumb.ffmpeg_missing",
            "Video thumbnails need ffmpeg, and it was not found on this system.",
            503,
        )

    try:
        data = _ffmpeg_frame(exe, src, VIDEO_SEEK_SECONDS)
        if not data:
            # 短於 1 秒的片子在第 1 秒沒有影格。退到第 0 秒再試一次 ——
            # 這是**業務邏輯**不是掩蓋：兩次都失敗仍然明確回 500。
            data = _ffmpeg_frame(exe, src, 0)
    except subprocess.TimeoutExpired as exc:
        raise ApiError(
            "thumb.ffmpeg_timeout",
            f"ffmpeg timed out after {FFMPEG_TIMEOUT} s.",
            500,
        ) from exc

    if not data:
        raise ApiError(
            "thumb.no_frame",
            "ffmpeg could not extract a frame - the original file may be broken.",
            500,
        )
    return data


def _ugoira_frame_bytes(src: Path) -> bytes:
    """ugoira（pixiv 的動圖）是一包 zip 裝一堆 jpg，取檔名排序的第一張。

    **不需要 ffmpeg** —— 這一項在沒裝 ffmpeg 的機器上也要成立。
    """
    try:
        with zipfile.ZipFile(src) as zf:
            names = sorted(n for n in zf.namelist() if not n.endswith("/"))
            if not names:
                raise ApiError("thumb.ugoira_empty", "The ugoira zip is empty.", 500)
            return zf.read(names[0])
    except zipfile.BadZipFile as exc:
        raise ApiError(
            "thumb.ugoira_unreadable", f"The ugoira zip cannot be opened: {exc}", 500,
        ) from exc


def _render_thumb(src: Path, dst: Path, *, data: bytes | None = None) -> None:
    """生一張縮圖到 `dst`。失敗就讓例外往上拋。

    `data` 有值時從記憶體讀（影片抽出來的影格、ugoira 的第一張），
    否則直接開 `src`。兩條路共用同一組尺寸與品質常數 ——
    分開寫的話，縮圖規格遲早會在兩邊漂移。

    ⚠️ **不 fallback 成佔位圖。** 生不出來多半代表原檔壞了或格式沒支援，
    回一張灰方塊等於把壞檔藏起來 —— 使用者會以為那張圖本來就長那樣，
    然後在幾個月後才發現一批檔案是壞的。
    """
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data) if data is not None else src) as im:
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


@router.api_route("/media/{media_id}/thumb", methods=FILE_METHODS)
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

    ### 影片與 ugoira

    影片抽第 1 秒那一格（ffmpeg），ugoira 取 zip 裡的第一張（不需要 ffmpeg）。
    兩者都經過併發閘 —— 一頁 60 格全是影片時，沒有閘就是 60 個 ffmpeg
    行程同時起來。

    ### 狀態碼的分工（前端靠它決定顯示什麼，不可混用）

    - 404 檔案不在了（被刪，或那顆碟沒插）
    - 415 這個格式**真的**做不出縮圖
    - 500 原檔壞了／抽不出影格
    - 503 依賴缺失（沒裝 ffmpeg）或排隊逾時 —— 這兩種都是「等一下或裝一下就好」
    """
    path = resolve_media_file(media_id, session, cfg)
    suffix = path.suffix.lower()

    is_video = suffix in VIDEO_SUFFIXES
    is_ugoira = suffix in UGOIRA_SUFFIXES
    if not (is_video or is_ugoira or suffix in THUMBABLE_SUFFIXES):
        # 415 而不是 404：檔案在，只是這種格式做不出縮圖。
        # 兩者混用的話，前端分不出「檔案不見了」與「這是不支援的格式」。
        raise ApiError("thumb.unsupported", f"No thumbnail can be made for {suffix}.", 415)

    cache = _thumb_cache_path(cfg, media_id)
    if cache.exists():
        return FileResponse(
            cache, media_type="image/webp", headers={"Cache-Control": IMMUTABLE}
        )

    if is_video or is_ugoira:
        # ⚠️ 閘只圈住「取得原始影格」與「縮圖」這一段，不圈快取命中那條路 ——
        # 已經生好的縮圖不該去排隊。
        gate = _gate(cfg)
        if not gate.acquire(timeout=THUMB_QUEUE_TIMEOUT):
            raise ApiError(
                "thumb.queue_timeout",
                "The thumbnail queue timed out; try again in a moment.",
                503,
            )
        try:
            data = (_video_frame_bytes(cfg, path) if is_video
                    else _ugoira_frame_bytes(path))
            _render_thumb_or_500(media_id, path, cache, data=data)
        finally:
            gate.release()
    else:
        _render_thumb_or_500(media_id, path, cache)

    return FileResponse(
        cache, media_type="image/webp", headers={"Cache-Control": IMMUTABLE}
    )


def _render_thumb_or_500(
    media_id: int, path: Path, cache: Path, *, data: bytes | None = None
) -> None:
    try:
        _render_thumb(path, cache, data=data)
    except HTTPException:
        raise            # 已經是講得清楚的狀態碼，不要蓋成 500
    except Exception as exc:  # noqa: BLE001
        log.warning("縮圖生成失敗 media#%s（%s）：%s", media_id, path, exc)
        raise ApiError("thumb.render_failed", f"Thumbnail rendering failed: {exc}", 500) from exc
