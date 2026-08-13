"""Mastodon adapter（baraag.net 等）。

**不用瀏覽器。** Mastodon 的 REST API 公開內容免認證即可讀。
需要認證的內容（追蹤者限定、部分成人站台的設定）帶 access token 即可。

API：
  GET /api/v1/accounts/lookup?acct=            -> account
  GET /api/v1/accounts/{id}/statuses?only_media=true&limit=&max_id=  -> status[]
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ..db.enums import MediaKind
from .base import (
    CONSERVATIVE_RATE_LIMIT,
    AuthRequired,
    FetchPage,
    NormalizedMedia,
    NormalizedPost,
    RemoteAccount,
)

# media_attachments[].type 是 Mastodon 自己的字串，不是 MIME
_TYPE_MAP = {
    "image": MediaKind.PHOTO.value,
    "video": MediaKind.VIDEO.value,
    # gifv 是「沒有聲音、會自動循環的短片」，Mastodon 存成 mp4。
    # 對應到 animated_gif 才符合使用者的認知。
    "gifv": MediaKind.ANIMATED_GIF.value,
}


class MastodonFieldError(ValueError):
    """回應少了必要欄位。缺欄位通常代表平台改版，明確報錯不兜過去。"""


class MastodonAuthRequired(AuthRequired):
    """這個帳號或這批內容需要認證才看得到。

    ⚠️ 只涵蓋 instance **明確回 401/403** 的情況。追蹤者限定的貼文在未認證時
    是回 200 加一份比較短的清單，那種軟性漏抓這裡偵測不到 —— 見 TASKS 的 E-d。
    """


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _need(obj: dict, key: str, where: str) -> Any:
    if key not in obj or obj[key] is None:
        raise MastodonFieldError(f"{where} 缺少 {key!r} —— Mastodon 可能改版了")
    return obj[key]


class MastodonAdapter:
    platform = "mastodon"

    # 沒有實測過 Mastodon 的 429 行為，所以吃最保守的預設（停止不重試）。
    # 想放寬要先有實測，不是猜。
    rate_limit_policy = CONSERVATIVE_RATE_LIMIT

    def auth_headers(self, cfg: Any, host: str) -> dict[str, str]:
        """Mastodon 吃 Bearer。公開內容不需要，baraag.net 之類的站台部分內容需要。

        沒設 token 就回空 dict —— 這**不是**兜底：公開內容本來就免認證，
        「沒有 token」是合法狀態，不是缺欄位。
        """
        token = (getattr(cfg, "instance_tokens", None) or {}).get(host)
        return {"Authorization": f"Bearer {token}"} if token else {}

    # ── 正規化 ──────────────────────────────────────────

    def normalize(self, payload: Any, instance_host: str = "") -> list[NormalizedPost]:
        if not isinstance(payload, list):
            raise TypeError("Mastodon payload 應該是 status 陣列")

        posts: list[NormalizedPost] = []
        for status in payload:
            # 轉嘟：媒體在 reblog 裡面，作者也是原作者。
            # 記成 is_retweet 但仍然抓 —— 要不要下載轉嘟是使用者的篩選條件，
            # 不是 adapter 該替他決定的。
            inner = status.get("reblog") or status
            is_reblog = status.get("reblog") is not None

            media: list[NormalizedMedia] = []
            for i, m in enumerate(inner.get("media_attachments") or []):
                kind = _TYPE_MAP.get(m.get("type"))
                if kind is None:
                    # audio / unknown。跳過，但不假裝它是圖片。
                    continue
                # `url` 是原始檔；`preview_url` 是縮圖，不可以拿來當來源。
                url = _need(m, "url", "status.media_attachments[]")
                media.append(
                    NormalizedMedia(
                        ordinal=i,
                        kind=kind,
                        source_url=url,
                        platform_media_key=m.get("id"),
                        meta={
                            k: m[k]
                            for k in ("preview_url", "description", "blurhash",
                                      "type", "remote_url")
                            if k in m
                        },
                    )
                )

            if not media:
                continue

            account = _need(inner, "account", "status")
            posts.append(
                NormalizedPost(
                    platform_post_id=str(_need(inner, "id", "status")),
                    platform_user_id=str(_need(account, "id", "status.account")),
                    instance_host=instance_host,
                    posted_at=_parse_date(inner.get("created_at")),
                    is_retweet=is_reblog,
                    media=media,
                    # 敏感標記在 **status 層**（與 Misskey 相反，那邊在檔案層）
                    sensitive_hint=bool(inner.get("sensitive")) or None,
                )
            )
        return posts

    def download_headers(self, url: str) -> dict[str, str]:
        return {"User-Agent": "SNSMediaDL/0.1"}

    # ── 抓取（SourceAdapter）─────────────────────────────

    async def resolve_account(
        self, client: httpx.AsyncClient, host: str, acct: str
    ) -> RemoteAccount:
        r = await client.get(
            f"https://{host}/api/v1/accounts/lookup",
            params={"acct": acct.lstrip("@")},
        )
        if r.status_code in (401, 403):
            raise MastodonAuthRequired(
                f"@{acct} 需要認證才查得到 —— 請在 config.toml 設定 {host} 的 token"
            )
        r.raise_for_status()
        data = r.json()
        return RemoteAccount(
            platform_user_id=str(_need(data, "id", "accounts/lookup")),
            screen_name=str(_need(data, "acct", "accounts/lookup")),
            instance_host=host,
        )

    async def resolve_account_by_id(
        self, client: httpx.AsyncClient, host: str, user_id: str
    ) -> RemoteAccount:
        """用 Mastodon 的 account id 解析。更新既有帳號走這條 —— 帳號會改名。"""
        r = await client.get(f"https://{host}/api/v1/accounts/{user_id}")
        if r.status_code in (401, 403):
            raise MastodonAuthRequired(
                f"帳號 {user_id} 需要認證才查得到 —— 請在 config.toml 設定 {host} 的 token"
            )
        r.raise_for_status()
        data = r.json()
        return RemoteAccount(
            platform_user_id=str(_need(data, "id", "accounts/{id}")),
            screen_name=str(_need(data, "acct", "accounts/{id}")),
            instance_host=host,
        )

    async def fetch_page(
        self,
        client: httpx.AsyncClient,
        account: RemoteAccount,
        cursor: str | None,
        limit: int,
    ) -> FetchPage:
        params: dict[str, Any] = {
            "only_media": "true",
            "limit": min(limit, 40),    # Mastodon 的上限
        }
        if cursor:
            params["max_id"] = cursor

        r = await client.get(
            f"https://{account.instance_host}"
            f"/api/v1/accounts/{account.platform_user_id}/statuses",
            params=params,
        )
        if r.status_code in (401, 403):
            raise MastodonAuthRequired(
                f"@{account.screen_name} 的貼文需要認證才讀得到 —— "
                "已停止列舉（不會假裝抓完了）"
            )
        r.raise_for_status()
        statuses = r.json()
        if not isinstance(statuses, list):
            raise MastodonFieldError("statuses 沒有回陣列 —— Mastodon 可能改版了")

        posts = self.normalize(statuses, instance_host=account.instance_host)
        # 游標用整頁最後一則的 id，不是濾過的 —— 濾過的會讓中間的貼文被跳過。
        next_cursor = str(statuses[-1]["id"]) if statuses else None
        return FetchPage(posts=posts, next_cursor=next_cursor)
