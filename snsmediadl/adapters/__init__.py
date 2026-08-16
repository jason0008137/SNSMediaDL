"""平台 adapter。核心流程共用，每個平台只實作自己的 adapter。

三種能力，**用「有沒有實作 Protocol」表示，不用旗標**：

- `PlatformAdapter`（全部都有）—— 正規化 + 下載 header + 認證 header + 429 政策
- `SourceAdapter` —— 游標式分頁列舉（Misskey / Mastodon）
- `IdListSource` —— 一次拿全部 id 再逐一問詳情（pixiv）

X 只有第一種：它的公開 API 已死，資料只能由 extension 推進來。

後兩者互斥：它們是**不同的列舉形狀**，不是同一個介面的兩種參數。
"""

from __future__ import annotations

from .base import (
    CONSERVATIVE_RATE_LIMIT,
    DEFAULT_CLIENT_PROFILE,
    ClientProfile,
    AuthRequired,
    FetchPage,
    IdListSource,
    MediaUrlRepair,
    NormalizedMedia,
    NormalizedPost,
    PlatformAdapter,
    RateLimitPolicy,
    RemoteAccount,
    SourceAdapter,
)
from .mastodon import MastodonAdapter
from .misskey import MisskeyAdapter
from .pixiv import PixivAdapter
from .x import XAdapter

_ADAPTERS: dict[str, PlatformAdapter] = {
    XAdapter.platform: XAdapter(),
    MisskeyAdapter.platform: MisskeyAdapter(),
    MastodonAdapter.platform: MastodonAdapter(),
    PixivAdapter.platform: PixivAdapter(),
}


def get_adapter(platform: str) -> PlatformAdapter:
    try:
        return _ADAPTERS[platform]
    except KeyError:
        raise ValueError(
            f"沒有 {platform!r} 的 adapter（目前支援：{sorted(_ADAPTERS)}）"
        ) from None


def get_source_adapter(platform: str) -> SourceAdapter | IdListSource:
    """要能自己抓的 adapter。X 走這裡會明確報錯 —— 它只能由 extension 推。

    接受兩種列舉能力中的任一種。呼叫端（`services/fetch.py`）再用
    `isinstance(adapter, IdListSource)` 決定走哪條路徑。
    """
    adapter = get_adapter(platform)
    if not isinstance(adapter, (SourceAdapter, IdListSource)):
        raise ValueError(
            f"{platform!r} 不能由 backend 主動抓取"
            "（它的資料來源是 extension，請在瀏覽器裡錄製）"
        )
    return adapter


__all__ = [
    "CONSERVATIVE_RATE_LIMIT",
    "DEFAULT_CLIENT_PROFILE",
    "ClientProfile",
    "AuthRequired",
    "FetchPage",
    "IdListSource",
    "MastodonAdapter",
    "MediaUrlRepair",
    "MisskeyAdapter",
    "NormalizedMedia",
    "NormalizedPost",
    "PixivAdapter",
    "PlatformAdapter",
    "RateLimitPolicy",
    "RemoteAccount",
    "SourceAdapter",
    "XAdapter",
    "get_adapter",
    "get_source_adapter",
]
