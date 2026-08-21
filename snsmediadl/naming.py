"""檔名產生：format token + Windows 安全化 + 衝突處理。

token 系統的概念移植自 twitter_media_downloader 的 `src/mapper.py`
（Spark-NF，Apache-2.0，https://github.com/Spark-NF/twitter_media_downloader ——
本機唯讀參考在 `RefRepo/`，不進版控）。歸屬聲明見根目錄 `NOTICE`。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from .fspath import for_io

TOKEN_RE = re.compile(r"%(\w+)%")

# Windows 不允許的字元 + 控制字元
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Windows 保留裝置名（不分大小寫，且「有副檔名也一樣被保留」）
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 單一路徑元件的長度上限。留餘裕給衝突序號與副檔名。
_MAX_COMPONENT = 120


def sanitize_component(name: str, fallback: str = "untitled") -> str:
    """把任意字串變成單一個安全的路徑元件（不含分隔符）。"""
    cleaned = _ILLEGAL.sub("_", name)
    # Windows 會自動剝掉結尾的點與空白，導致「寫入的檔名」與「實際檔名」不符
    cleaned = cleaned.rstrip(" .")
    cleaned = cleaned.strip()

    if len(cleaned) > _MAX_COMPONENT:
        cleaned = cleaned[:_MAX_COMPONENT].rstrip(" .")

    if not cleaned:
        return fallback

    stem = cleaned.split(".")[0].upper()
    if stem in _RESERVED:
        cleaned = f"_{cleaned}"

    return cleaned


def split_url_filename(url: str) -> tuple[str, str]:
    """從 URL 取出 (檔名主體, 副檔名)。query string 必須先剝掉才推得對。"""
    path = PurePosixPath(urlparse(url).path)
    ext = path.suffix.lstrip(".").lower()
    return path.stem, ext


def _default_ext(kind: str) -> str:
    return "jpg" if kind == "photo" else "mp4"


def build_tokens(
    *,
    platform: str,
    post_id: str,
    ordinal: int,
    kind: str,
    source_url: str,
    posted_at: datetime | None = None,
    user_id: str = "",
    screen_name: str = "",
) -> dict[str, str]:
    stem, ext = split_url_filename(source_url)
    return {
        "platform": platform,
        "post_id": post_id,
        "ordinal": str(ordinal),
        "kind": kind,
        "filename": stem or post_id,
        "ext": ext or _default_ext(kind),
        "user_id": user_id,
        "user_screen_name": screen_name or user_id,
        "date": posted_at.strftime("%Y-%m-%d %H-%M-%S") if posted_at else "unknown-date",
    }


def render_filename(fmt: str, tokens: dict[str, str]) -> str:
    """套用 format 字串。未知 token 原樣保留，方便使用者發現自己打錯。"""
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return tokens.get(key, match.group(0))

    return sanitize_component(TOKEN_RE.sub(repl, fmt))


def resolve_collision(target: Path) -> Path:
    r"""檔名已被佔用時加 _1 / _2。不覆寫既有檔案。

    ⚠️ `exists()` 走 `fspath.for_io`。少了前綴的話，超過 260 字元的目標會被
    Windows 說成「不存在」—— 於是這支函式回報「沒有衝突」，接著寫入端
    **直接覆蓋掉一個已經在的檔案**。這比看不到圖嚴重得多：使用者會少一張圖，
    而且沒有任何訊息。回傳的仍是一般路徑（那是要存進 DB 的形狀）。
    """
    if not for_io(target).exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(1, 10_000):
        candidate = target.with_name(f"{stem}_{i}{suffix}")
        if not for_io(candidate).exists():
            return candidate
    raise RuntimeError(f"檔名衝突無法解決：{target}")


def build_target_path(
    *,
    output_root: Path,
    fmt: str,
    tokens: dict[str, str],
    group_by_account: bool = True,
) -> Path:
    """組出最終落檔路徑：<root>/<platform>/<screen_name>/<檔名>。"""
    parts = [output_root]
    if group_by_account:
        parts.append(Path(sanitize_component(tokens["platform"], "unknown-platform")))
        parts.append(
            Path(sanitize_component(tokens["user_screen_name"], "unknown-account"))
        )
    directory = Path(*parts)
    return directory / render_filename(fmt, tokens)
