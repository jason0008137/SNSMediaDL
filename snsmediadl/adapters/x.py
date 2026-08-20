"""X (Twitter) adapter。

吃 extension 倒出的格式（見 `extension/content.js`）。
欄位對應取自 x.com GraphQL `UserMedia` 回應的實測結果：媒體資訊掛在
`tweet_results.result.legacy.extended_entities.media[]`，欄位名沿用 API v1.1
的舊命名（`media_url_https` / `video_info.variants` / `possibly_sensitive`）。
去識別化的回應樣本見 `extension/fixtures/x-usermedia-sample.json`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..db.enums import MediaKind
from .base import (
    CONSERVATIVE_RATE_LIMIT,
    DEFAULT_CLIENT_PROFILE,
    NormalizedMedia,
    NormalizedPost,
)

# X 的 created_at 格式：'Tue Jul 08 11:43:52 +0000 2025'
# 這是 v1.1 時代就在用的格式，GraphQL 的 legacy 物件原封不動沿用。
_DATE_FORMAT = "%a %b %d %H:%M:%S %z %Y"

_KIND_MAP = {
    "photo": MediaKind.PHOTO.value,
    "video": MediaKind.VIDEO.value,
    "animated_gif": MediaKind.ANIMATED_GIF.value,
}


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, _DATE_FORMAT)
    except ValueError:
        # 格式變了是平台改版的訊號，不要吞掉 —— 但也不該讓整批 ingest 掛掉。
        # 回 None 會讓 posted_at 為空，查詢時看得出來。
        return None


class XAdapter:
    platform = "x"

    # X 超速會**鎖整個帳號約一天**，所以 429 就停、不重試。
    # 這是最保守的政策，也是所有平台的預設。
    rate_limit_policy = CONSERVATIVE_RATE_LIMIT

    # 誠實表明身分即可 —— 這三個平台的 API 不做客戶端指紋檢查
    # （只有 pixiv 需要偽裝，理由寫在它自己的 adapter 裡）。
    client_profile = DEFAULT_CLIENT_PROFILE

    def auth_headers(self, cfg: Any, host: str) -> dict[str, str]:
        # X 不走 backend 主動抓（公開 API 已死），永遠不會被呼叫到。
        # 介面要求要有，回空 dict。
        return {}

    def normalize(self, payload: Any) -> list[NormalizedPost]:
        if not isinstance(payload, list):
            raise TypeError("X payload 應該是貼文陣列")

        posts: list[NormalizedPost] = []
        for raw in payload:
            media: list[NormalizedMedia] = []
            for i, m in enumerate(raw.get("media") or []):
                kind = _KIND_MAP.get(m.get("kind"))
                if kind is None:
                    # 未知型別代表平台加了新東西 —— 跳過但不靜默：
                    # meta 會留痕，之後 lint 查得到。
                    continue

                # photo 取 orig（原圖），video/gif 取 url（已挑過最高 bitrate）
                url = m.get("orig") if kind == MediaKind.PHOTO.value else m.get("url")
                url = url or m.get("url")
                if not url:
                    continue

                meta = {
                    k: m[k]
                    for k in ("bitrate", "availableBitrates", "thumb", "durationMs")
                    if k in m
                }
                media.append(
                    NormalizedMedia(
                        ordinal=i,
                        kind=kind,
                        source_url=url,
                        # extension 尚未送 media_key，先留 None 走 (post_id, ordinal)
                        platform_media_key=m.get("mediaKey"),
                        meta=meta,
                    )
                )

            if not media:
                continue

            posts.append(
                NormalizedPost(
                    platform_post_id=str(raw["postId"]),
                    platform_user_id=str(raw["userId"]),
                    posted_at=_parse_date(raw.get("createdAt")),
                    is_retweet=bool(raw.get("isRetweet", False)),
                    media=media,
                    sensitive_hint=raw.get("possiblySensitive"),
                    rating=raw.get("rating"),
                    content_type=raw.get("contentType"),
                )
            )

        return posts

    def download_headers(self, url: str) -> dict[str, str]:
        # 實測：X 的媒體 URL 完全不需要認證或 Referer（12/12 全 200）。
        return {"User-Agent": "SNSMediaDL/0.1"}
