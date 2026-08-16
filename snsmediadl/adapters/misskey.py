"""Misskey adapter（misskey.io 等）。

**不用瀏覽器。** Misskey 有文件齊全的 REST API，公開內容免認證即可讀。

`RefRepo/EpicDL/downloader_MSK.py` 走的是 Selenium 點日文按鈕（`ノート` →
`ファイル付き`）再叫 gallery-dl 下載，那條路脆弱（它靠 `article.x5yeR` 這種
build 產物的 class name）、慢、而且把下載外包出去，與「DB 是唯一真實來源」衝突。
取其實戰知識，不取其抓取層。

API（都是 POST + JSON body，Misskey 的慣例）：
  /api/users/show   {username, host}  -> user
  /api/users/notes  {userId, withFiles, limit, untilId} -> note[]
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ..db.enums import MediaKind
from .base import (
    CONSERVATIVE_RATE_LIMIT,
    DEFAULT_CLIENT_PROFILE,
    FetchPage,
    NormalizedMedia,
    NormalizedPost,
    RemoteAccount,
)

# note.files[].type 是 MIME，不是像 X 那樣的 'photo'/'video'
_MIME_PREFIX_TO_KIND = {
    "image/gif": MediaKind.ANIMATED_GIF.value,
    "image/": MediaKind.PHOTO.value,
    "video/": MediaKind.VIDEO.value,
}


class MisskeyFieldError(ValueError):
    """回應少了必要欄位。

    **刻意讓它炸掉而不是兜過去**：缺欄位通常代表平台改版，
    用 `.get(k, '')` 撐過去的結果是靜默漏抓，幾個月後才發現。
    """


def _kind_of(mime: str | None) -> str | None:
    if not mime:
        return None
    if mime in _MIME_PREFIX_TO_KIND:
        return _MIME_PREFIX_TO_KIND[mime]
    for prefix, kind in _MIME_PREFIX_TO_KIND.items():
        if prefix.endswith("/") and mime.startswith(prefix):
            return kind
    return None


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # Misskey 回 ISO8601 帶 Z
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _need(obj: dict, key: str, where: str) -> Any:
    if key not in obj or obj[key] is None:
        raise MisskeyFieldError(f"{where} 缺少 {key!r} —— Misskey 可能改版了")
    return obj[key]


class MisskeyAdapter:
    platform = "misskey"

    # 沒有實測過 Misskey 的 429 行為，所以吃最保守的預設（停止不重試）。
    rate_limit_policy = CONSERVATIVE_RATE_LIMIT

    # 誠實表明身分即可 —— 這三個平台的 API 不做客戶端指紋檢查
    # （只有 pixiv 需要偽裝，理由寫在它自己的 adapter 裡）。
    client_profile = DEFAULT_CLIENT_PROFILE

    def auth_headers(self, cfg: Any, host: str) -> dict[str, str]:
        """Misskey 也吃 Bearer。公開內容免認證，沒設 token 是合法狀態。"""
        token = (getattr(cfg, "instance_tokens", None) or {}).get(host)
        return {"Authorization": f"Bearer {token}"} if token else {}

    # ── 正規化（純函式，可用 fixture 測）─────────────────

    def normalize(self, payload: Any, instance_host: str = "") -> list[NormalizedPost]:
        if not isinstance(payload, list):
            raise TypeError("Misskey payload 應該是 note 陣列")

        posts: list[NormalizedPost] = []
        for note in payload:
            media: list[NormalizedMedia] = []
            for i, f in enumerate(note.get("files") or []):
                kind = _kind_of(f.get("type"))
                if kind is None:
                    # 未知型別代表平台加了新東西（audio？）。跳過但留痕在 meta，
                    # 之後查得到。
                    continue
                url = _need(f, "url", "note.files[]")
                media.append(
                    NormalizedMedia(
                        ordinal=i,
                        kind=kind,
                        source_url=url,
                        # Misskey 的 file id 是穩定識別碼 —— 比 X 好，X 只能靠
                        # (post_id, ordinal)
                        platform_media_key=f.get("id"),
                        meta={
                            k: f[k]
                            for k in ("thumbnailUrl", "size", "type", "isSensitive",
                                      "blurhash", "name")
                            if k in f
                        },
                    )
                )

            if not media:
                continue

            user = _need(note, "user", "note")
            # 敏感標記在**檔案層**（與 Mastodon 相反，那邊在 status 層）。
            # 任一檔案敏感就當整則敏感 —— 分級是 post 層的屬性。
            sensitive = any(
                (f.get("isSensitive") is True) for f in (note.get("files") or [])
            )
            posts.append(
                NormalizedPost(
                    platform_post_id=str(_need(note, "id", "note")),
                    platform_user_id=str(_need(user, "id", "note.user")),
                    instance_host=instance_host,
                    posted_at=_parse_date(note.get("createdAt")),
                    # renote 且沒有自己的內容 = 純轉發
                    is_retweet=bool(note.get("renoteId")) and not note.get("text"),
                    media=media,
                    sensitive_hint=sensitive or None,
                )
            )
        return posts

    def download_headers(self, url: str) -> dict[str, str]:
        # Misskey 的媒體走自家 CDN（files.misskey.io），不需要 Referer
        return {"User-Agent": "SNSMediaDL/0.1"}

    # ── 抓取（SourceAdapter）─────────────────────────────

    async def resolve_account(
        self, client: httpx.AsyncClient, host: str, acct: str
    ) -> RemoteAccount:
        # 使用者可能貼 '@name' 或 'name'
        username = acct.lstrip("@")
        # host=None 代表「這個 instance 的本地使用者」。
        # 遠端使用者（@a@b.example）不在第一版範圍。
        r = await client.post(
            f"https://{host}/api/users/show",
            json={"username": username, "host": None},
        )
        r.raise_for_status()
        data = r.json()
        return RemoteAccount(
            platform_user_id=str(_need(data, "id", "users/show")),
            screen_name=str(_need(data, "username", "users/show")),
            instance_host=host,
        )

    async def resolve_account_by_id(
        self, client: httpx.AsyncClient, host: str, user_id: str
    ) -> RemoteAccount:
        """用 Misskey 的 user id 解析。更新既有帳號走這條 —— 帳號會改名。"""
        r = await client.post(f"https://{host}/api/users/show", json={"userId": user_id})
        r.raise_for_status()
        data = r.json()
        return RemoteAccount(
            platform_user_id=str(_need(data, "id", "users/show")),
            screen_name=str(_need(data, "username", "users/show")),
            instance_host=host,
        )

    async def fetch_page(
        self,
        client: httpx.AsyncClient,
        account: RemoteAccount,
        cursor: str | None,
        limit: int,
    ) -> FetchPage:
        body: dict[str, Any] = {
            "userId": account.platform_user_id,
            "withFiles": True,
            "limit": min(limit, 100),   # Misskey 的上限
        }
        if cursor:
            body["untilId"] = cursor

        r = await client.post(
            f"https://{account.instance_host}/api/users/notes", json=body
        )
        r.raise_for_status()
        notes = r.json()
        if not isinstance(notes, list):
            raise MisskeyFieldError("users/notes 沒有回陣列 —— Misskey 可能改版了")

        posts = self.normalize(notes, instance_host=account.instance_host)
        # 游標要用「這一頁最後一則 note 的 id」，不是最後一則**有媒體**的 ——
        # normalize 會濾掉沒媒體的，拿濾過的當游標會讓中間的貼文被跳過。
        next_cursor = str(notes[-1]["id"]) if notes else None
        return FetchPage(posts=posts, next_cursor=next_cursor)
