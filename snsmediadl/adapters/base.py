"""Adapter 介面與正規化後的資料形狀。

抓取層假設「隨時會壞」：易碎的部分（endpoint、欄位名、DOM）全部關在 adapter 裡，
平台改版時只需要改一個檔案。
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class AuthRequired(RuntimeError):
    """這批內容需要認證才看得到，列舉已停止。

    **不可以當成「沒有內容」靜默跳過** —— 那是典型的靜默漏抓：
    你會以為抓完了，其實只拿到公開的那部分。

    放在 base 而不是各 adapter 自己定義，是為了讓服務層與 CLI 能接住它
    而不必認得任何一個平台。各 adapter 仍可繼承出自己的子類別加細節。
    """


@dataclass(frozen=True)
class RateLimitPolicy:
    """碰到 HTTP 429 時該怎麼辦。**這是平台屬性，不是全域設定。**

    X 與 pixiv 的正確處置完全相反：
      - X 超速會**鎖整個帳號約一天**，所以 429 就停、不重試（預設值）
      - pixiv 的 429 是可恢復的暫時限制，PBD 的實務是等 200 秒再試

    把任何一邊的政策套到另一邊都是錯的，所以它掛在 adapter 上。
    """

    # 429 之後重試幾次。0 = 不重試（X 的政策）。
    max_retries: int = 0
    # 每次重試前等多久（秒）。
    wait_seconds: float = 0.0

    # 任兩次**下載開始**的最小間隔（秒）。0 = 不節流。
    #
    # 這一條原本是全域設定（`Config.download_delay_seconds`），但它的理由
    # 從頭到尾都是 X 的：「超速會鎖整個帳號約一天」。把 X 的政策套到 pixiv
    # 身上的代價很具體 —— 併發 4 個 worker 被序列化成每秒 1 個檔，
    # semaphore 等於失效。
    #
    # 已求證（RefRepo/PixivBatchDownloader，2026-08-20）：pixiv 的限速在
    # **列舉那一段**，不在媒體 CDN。PBD 的 slowCrawl（1800ms）只包住
    # `getWorksData()` 這個作品資料 API；下載端是 6 條並行、任何一條完成或
    # 失敗都**立刻**補下一個，中間沒有延遲。
    #
    # ⚠️ 這是**平台屬性**，跟上面兩個欄位同一個道理：把任何一邊的值套到
    # 另一邊都是錯的。預設保守（1 秒）—— 新平台要加速必須先拿出證據。
    download_delay_seconds: float = 1.0

    # ⚠️ 刻意**沒有**「要不要停」這個旗標：重試次數用完了還是 429，
    # 就代表真的被限速了，繼續打正是把「暫時限速」變成「帳號鎖定」的方式。
    # 所以行為固定是「停掉該平台本輪剩下的工作」，差別只在停之前試幾次。
    #
    # 也刻意不抄 PBD 的無限重試：對背景佇列來說，「無限次等 200 秒」
    # 的使用者體感是佇列卡死，不是錯誤。


# 預設 = X 的政策（最保守）。新平台沒想清楚之前就吃這個。
CONSERVATIVE_RATE_LIMIT = RateLimitPolicy()


@dataclass(frozen=True)
class ClientProfile:
    """這個平台的 API 期待對面是**什麼樣的 HTTP 客戶端**。

    ### 為什麼需要這個東西

    2026-08-16 實測：`www.pixiv.net/ajax/*` 走在 Cloudflare 後面，會對
    httpx 的預設連線回 **403 + `Just a moment...` 挑戰頁**。逐項對照實驗：

    | 條件 | 結果 |
    |------|------|
    | 預設 SSLContext + 任何 User-Agent | 403 |
    | ALPN 設成 `h2, http/1.1` | 沒有差別 |
    | **Chrome 的 cipher 順序 + 瀏覽器 UA** | **200** |
    | Chrome cipher 但 UA 是 `SNSMediaDL/0.1` | 403 |

    也就是 Cloudflare 同時看 **TLS ClientHello 指紋（JA3）**與 User-Agent，
    兩個條件缺一不可。系統 curl（Schannel）拿得到 200，Python 的 `ssl`
    模組（OpenSSL）預設 cipher 順序拿不到 —— 差別就在這裡。

    ### 為什麼不放在 `auth_headers`

    這不是認證：沒有憑證的請求一樣被擋（實測未帶 cookie 也是 403）。
    它是「連線長什麼樣」，與 `RateLimitPolicy` 同一個層級的平台屬性，
    所以用同樣的形式 —— **每個 adapter 明寫，不給 getattr 預設**。

    ### 只影響平台 API，不影響 CDN

    `i.pximg.net` 用預設連線 + `SNSMediaDL/0.1` 抓得到（實測 17 MB 原圖 200）。
    下載層維持原樣，**不要**把瀏覽器 UA 一起套過去 —— 那會讓「哪一層需要
    偽裝」這件事變得看不出來。
    """

    user_agent: str = "SNSMediaDL/0.1"
    # 回一個 SSLContext，或 None 代表用 httpx 的預設。
    # 用 factory 而不是直接放 SSLContext：建構它要載入系統 CA store，
    # 沒用到的平台不該在 import 時付這個成本。
    ssl_context_factory: Callable[[], ssl.SSLContext] | None = None

    def ssl_context(self) -> ssl.SSLContext | None:
        return self.ssl_context_factory() if self.ssl_context_factory else None

    def client_kwargs(self) -> dict[str, Any]:
        """給 `httpx.AsyncClient(**...)`。

        `verify` 只在真的有 context 時才給 —— httpx 的預設值是 `True`，
        傳 `None` 會被當成「不驗證憑證」。
        """
        ctx = self.ssl_context()
        return {"verify": ctx} if ctx is not None else {}


# 預設 = 誠實地說自己是誰。**這才是預設值**，偽裝成瀏覽器是例外，
# 而例外要在 adapter 上寫明理由（見 pixiv）。
DEFAULT_CLIENT_PROFILE = ClientProfile()


class NormalizedMedia(BaseModel):
    """單一媒體檔。kind 掛在這裡不掛 post —— 一則貼文可混合多種型別。"""

    ordinal: int
    kind: str
    source_url: str
    platform_media_key: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class NormalizedPost(BaseModel):
    platform_post_id: str
    platform_user_id: str
    # Fediverse 的 instance（misskey.io、baraag.net）。單一站台的平台留空字串。
    # 不可以是 None —— 它會進唯一鍵，而 SQLite 的 NULL 在唯一索引裡彼此不相等。
    instance_host: str = ""
    posted_at: datetime | None = None
    is_retweet: bool = False
    media: list[NormalizedMedia] = Field(default_factory=list)

    # 平台給的分級線索（X 的 possibly_sensitive）。弱訊號，只當 auto 猜測用。
    sensitive_hint: bool | None = None

    # 採集端明確帶來的分級。有值就是最高優先，記成 manual。
    rating: str | None = None
    content_type: str | None = None


@runtime_checkable
class PlatformAdapter(Protocol):
    platform: str

    # 429 政策。**每個 adapter 都要明寫**，不給 getattr 預設 ——
    # 忘了寫就應該在啟動時炸掉，而不是靜默套用別的平台的政策。
    rate_limit_policy: RateLimitPolicy

    # 連線長什麼樣（User-Agent + TLS 指紋）。同樣每個 adapter 明寫，
    # 理由與上面那條一樣：靜默套用別的平台的偽裝程度是錯的。
    client_profile: ClientProfile

    def normalize(self, payload: Any) -> list[NormalizedPost]:
        """把採集到的原始 payload 轉成 domain 物件。"""
        ...

    def download_headers(self, url: str) -> dict[str, str]:
        """下載該平台媒體時要帶的 header。

        X 不需要任何 header（實測 12/12 無認證可下載）；
        pixiv 要回 `Referer`（`i.pximg.net` 沒有 Referer 會 403）。

        ⚠️ **這裡不要放憑證。** pixiv 的 cookie 只有 `www.pixiv.net` 的 API 要，
        CDN 不要 —— 憑證應該只活在列舉階段。
        """
        ...

    def auth_headers(self, cfg: Any, host: str) -> dict[str, str]:
        """列舉階段要帶的認證 header。不需要認證的平台回空 dict。

        ⚠️ 這個方法存在的理由：認證方式是**平台知識**。
        原本 `services/fetch.py` 直接寫死「Bearer + instance_tokens[host]」，
        那是 Fediverse 的作法；pixiv 要的是 `Cookie: PHPSESSID=...`。
        平台無關的服務層不該認得任何一種。
        """
        ...


class RemoteAccount(BaseModel):
    """在遠端站台上查到的帳號。"""

    platform_user_id: str
    screen_name: str
    instance_host: str


class FetchPage(BaseModel):
    """列舉的一頁。`next_cursor` 為 None 代表沒有下一頁了。"""

    posts: list[NormalizedPost] = Field(default_factory=list)
    next_cursor: str | None = None


@runtime_checkable
class SourceAdapter(Protocol):
    """**能自己去平台抓資料**的 adapter。

    X 沒有這個能力 —— 它的公開 API 已死，唯一能看到已認證回應的地方是登入中的
    頁面本身，所以資料只能由 extension 推進來。Misskey 與 Mastodon 相反：
    兩者都有文件齊全、公開內容免認證的 REST API，backend 直接抓即可。

    ⚠️ 能力用「有沒有實作這個 Protocol」表示，**不要在 adapter 上放
    `can_fetch = False` 之類的旗標** —— 那會讓每個呼叫點都得寫 if。
    呼叫端用 `isinstance(adapter, SourceAdapter)` 判斷。
    """

    platform: str

    async def resolve_account(
        self, client: Any, host: str, acct: str
    ) -> RemoteAccount:
        """把 `@名稱` 換成平台的 user id。查不到要丟例外，不可以回 None 讓上層猜。"""
        ...

    async def resolve_account_by_id(
        self, client: Any, host: str, user_id: str
    ) -> RemoteAccount:
        """用**平台的 user id** 解析。

        ⚠️ 更新既有帳號一律走這條，不要用 `screen_name`：帳號改名是常態
        （`upsert_account` 本來就假設會改），拿舊名字去查會 404，
        那個帳號從此就再也更新不到了。
        """
        ...

    async def fetch_page(
        self, client: Any, account: RemoteAccount, cursor: str | None, limit: int
    ) -> FetchPage:
        """列舉一頁「有媒體的」貼文。分頁一律用平台自己的游標，不用時間戳 ——
        置頂與編輯過的貼文會讓時間序不可靠。"""
        ...


@runtime_checkable
class IdListSource(Protocol):
    """**一次就拿得到「全部作品 id」**的平台（pixiv）。

    與 `SourceAdapter` 的游標分頁互斥 —— 兩者是不同的列舉形狀，不是同一個介面
    的兩種參數。硬把 pixiv 塞進 `fetch_page(cursor)` 會失去它唯一的結構優勢：

        游標式：抓下來才知道抓過了
        id 清單：**先知道，再決定抓不抓**

    pixiv 的 `profile/all` 一個請求回全部 id，跟 DB 一比就知道要抓哪些，
    所以增量可以在**發出任何詳情請求之前**完成。在 1.8 秒一個請求的節流下，
    這個差別是幾十分鐘。

    ⚠️ 能力一樣用「有沒有實作」表示，不放旗標（理由見 `SourceAdapter`）。
    Misskey / Mastodon 沒有 `list_work_ids`，所以 isinstance 自然為 False。
    """

    platform: str

    async def resolve_account(
        self, client: Any, host: str, acct: str
    ) -> RemoteAccount: ...

    async def resolve_account_by_id(
        self, client: Any, host: str, user_id: str
    ) -> RemoteAccount: ...

    async def list_work_ids(self, client: Any, account: RemoteAccount) -> list[str]:
        """全部作品 id，新到舊。**一個請求**。"""
        ...

    async def fetch_works(
        self, client: Any, account: RemoteAccount, ids: list[str]
    ) -> list[NormalizedPost]:
        """逐一取詳情。

        ⚠️ **節流由 adapter 自己負責**：請求間隔是平台屬性，不是全域設定。
        pixiv 是 1.8 秒 + 併發 1（兩者缺一不可，只做間隔不做併發等於沒做）。
        """
        ...

    def estimate_seconds(self, work_count: int) -> float:
        """抓這麼多作品大概要多久。

        存在的理由：3000 個作品要跑 90 分鐘，**使用者必須在按下去之前就知道**。
        不能按下去然後不知道要等多久。
        """
        ...


@runtime_checkable
class MediaUrlRepair(Protocol):
    """媒體網址是**推導**出來的平台，需要在推導失效時能查到真的網址。

    只有 pixiv 有這個問題：多頁作品的第 2 頁以後是把 `_p0` 換成 `_pN` 推導的
    （PBD 的作法，一個作品省下一次請求）。推導對的時候很划算，錯的時候是 404。

    ⚠️ **這不是「找不到就退而求其次」的兜底。** 校正成功也要寫 WARNING ——
    它代表 pximg 的 URL 樣式變了，是需要有人知道的事實，不是修好就算了。
    """

    platform: str

    async def repair_media_url(
        self, client: Any, platform_media_key: str, source_url: str
    ) -> str:
        """查出這個媒體真正的網址。查不到要丟例外，不可以回原網址讓上層再撞一次。"""
        ...
