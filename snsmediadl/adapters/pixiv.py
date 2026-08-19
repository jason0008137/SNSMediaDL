"""pixiv adapter。

**不用瀏覽器、不用 extension。** pixiv 有一組文件外但穩定的 ajax API，
帶著登入 cookie（`PHPSESSID`）就能用。

參考實作是 `xuejianxianzun/PixivBatchDownloader`（MIT）——
取它的**節流數字**與**兩段式設計**，不取它的功能廣度。
https://github.com/xuejianxianzun/PixivBatchDownloader

⚠️ **列舉形狀跟其他平台都不一樣**：不是游標分頁，是「一次拿全部 id、
再逐一問詳情」。所以實作的是 `IdListSource` 而不是 `SourceAdapter`。

    GET /ajax/user/{id}/profile/all     -> 全部作品 id（1 個請求）
    GET /ajax/illust/{id}               -> 詳情（N 個請求，每個間隔 1.8 秒）
    GET /ajax/illust/{id}/ugoira_meta   -> 動圖幀資料（只有動圖才問）
    GET /ajax/illust/{id}/pages         -> 只在 URL 推導失效時當校正用

兩個 host 的權責分得很乾淨，**不要混**：
  - `www.pixiv.net`（API）要 cookie，不要 Referer
  - `i.pximg.net`（CDN）要 Referer，**不要 cookie**
"""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from datetime import datetime
from typing import Any

import httpx

from ..db.enums import MediaKind
from .base import (
    ClientProfile,
    NormalizedMedia,
    NormalizedPost,
    RateLimitPolicy,
    RemoteAccount,
)

log = logging.getLogger("snsmediadl.pixiv")

API_ROOT = "https://www.pixiv.net"
REFERER = "https://www.pixiv.net/"

# ── Cloudflare：`www.pixiv.net` 的 ajax API 擋 Python 的預設連線 ──────────
#
# 2026-08-16 實測（逐項對照，見 `ClientProfile` 的表格）：Cloudflare 同時看
# **TLS ClientHello 指紋**與 **User-Agent**，兩者缺一就回 403 挑戰頁。
# 症狀特別惡劣 —— 回的是 HTML 不是 JSON，而且和「帳號不存在」長得不像，
# 使用者只會看到一大串 `<!DOCTYPE html>`。
#
# ⚠️ 這兩個常數是**成對**的，不要只改一個：只換 UA 或只換 cipher 都是 403。

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)

# Chrome 的 cipher 順序。OpenSSL 預設的順序與這個不同，而 Cloudflare 認的
# 就是這個順序（JA3 把 cipher 清單也算進指紋）。
#
# ⚠️ 起作用的是 **TLS 1.2 那一段**：Chrome 把 AES128 排在 AES256 前面，
# OpenSSL 預設相反。最前面那三個 TLS 1.3 套件其實**排不動** ——
# Python 的 `set_ciphers()` 只管 1.2 以下，1.3 要 `SSL_CTX_set_ciphersuites`，
# `ssl` 模組沒開放。列在這裡是為了完整表達 Chrome 的清單，不是因為它有效。
# 「順手整理」這一串會讓 pixiv 變回 403，測試釘住了這件事。
_CHROME_CIPHERS = ":".join([
    "TLS_AES_128_GCM_SHA256",
    "TLS_AES_256_GCM_SHA384",
    "TLS_CHACHA20_POLY1305_SHA256",
    "ECDHE-ECDSA-AES128-GCM-SHA256",
    "ECDHE-RSA-AES128-GCM-SHA256",
    "ECDHE-ECDSA-AES256-GCM-SHA384",
    "ECDHE-RSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-CHACHA20-POLY1305",
    "ECDHE-RSA-CHACHA20-POLY1305",
    "ECDHE-RSA-AES128-SHA",
    "ECDHE-RSA-AES256-SHA",
    "AES128-GCM-SHA256",
    "AES256-GCM-SHA384",
    "AES128-SHA",
    "AES256-SHA",
])

_ssl_context: ssl.SSLContext | None = None


def chrome_ssl_context() -> ssl.SSLContext:
    """帶 Chrome cipher 順序的 TLS 設定。建一次就重複用。

    **憑證驗證照常** —— 這裡改的只有 cipher 順序，沒有降低任何安全性。
    """
    global _ssl_context
    if _ssl_context is None:
        ctx = ssl.create_default_context()
        ctx.set_ciphers(_CHROME_CIPHERS)
        _ssl_context = ctx
    return _ssl_context


PIXIV_CLIENT_PROFILE = ClientProfile(
    user_agent=_BROWSER_UA, ssl_context_factory=chrome_ssl_context
)

# 取自 PBD 的 `slowCrawlDealy`（原碼拼字如此）。PBD 強制下限 1000ms。
DETAIL_DELAY_SECONDS = 1.8

# pixiv 的 illustType。0=插畫 1=漫畫 2=動圖(ugoira)
ILLUST_TYPE_UGOIRA = 2

# 只換副檔名前面那個 `_p0`，避免作品 id 裡剛好有 "_p0" 之類的巧合誤傷。
_P0_SUFFIX = re.compile(r"_p0(?=\.[A-Za-z0-9]+$)")


class PixivFieldError(ValueError):
    """回應少了必要欄位，或形狀不符預期。

    **刻意讓它炸掉而不是兜過去**：缺欄位通常代表平台改版，
    用 `.get(k, '')` 撐過去的結果是靜默漏抓，幾個月後才發現。
    """


class PixivNotFound(PixivFieldError):
    """這個 user id 在 pixiv 上不存在（刪除或從來沒有過）。

    ⚠️ **只在 recon 驗證過的形狀上丟出**，因為它會餵給自動退訂
    （連續 2 次就把帳號移出追蹤名單）。判寬了，pixiv 改一次版就會把
    一整批還活著的帳號退訂掉 —— 那比不退訂糟得多。

    驗證過的形狀（2026-08-18，見 recon 筆記「帳號不存在長什麼樣」）：
    **HTTP 404 + 合法 JSON + `error: true`**，三者缺一不算。

    - 端點被移掉（平台改版）回的是 HTML 404 → 不合法 JSON → 不是這個型別
    - Cloudflare 擋掉是 HTML 403 → 連 404 都不是
    - **凍結帳號未取樣** → 若它是別種形狀，會落到 `PixivFieldError`／`FAILED`，
      不會被誤判成「不存在」

    訊息文字**刻意不比對**：實測 `Accept-Language` 無效，語言跟著帳號設定走，
    換一組 cookie 就換一種語言，比對必然靜默失效。
    """


def raise_if_not_found(response: httpx.Response, where: str) -> None:
    """404 + JSON + `error: true` → `PixivNotFound`。其餘一律不動。

    放在 `raise_for_status()` **之前**呼叫 —— pixiv 的「找不到」是真的 404，
    不是本檔上面說的那種「200 + error」，所以 `_body()` 這條路走不到。
    """
    if response.status_code != 404:
        return
    try:
        payload = response.json()
    except ValueError:
        # HTML 的 404 = 端點沒了 = 平台改版。**不可以當成帳號不存在**，
        # 那會在改版當天把所有 pixiv 帳號退訂。留給 raise_for_status()。
        return
    if isinstance(payload, dict) and payload.get("error"):
        raise PixivNotFound(
            f"{where}：這個 pixiv 使用者不存在或已離開 pixiv（HTTP 404）"
        )


def _need(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, dict):
        raise PixivFieldError(f"{where} 應該是物件，實際是 {type(obj).__name__}")
    if key not in obj or obj[key] is None:
        raise PixivFieldError(f"{where} 缺少 {key!r} —— pixiv 可能改版了")
    return obj[key]


def _body(payload: Any, where: str) -> Any:
    """取出 pixiv 回應的 `body`，順便把它自己回報的錯誤翻成例外。

    pixiv 的錯誤是 **HTTP 200 + `error: true`**，`raise_for_status()` 抓不到。
    不在這裡攔，錯誤訊息就會被當成資料往下流。
    """
    if not isinstance(payload, dict):
        raise PixivFieldError(f"{where} 應該回物件，實際是 {type(payload).__name__}")
    if payload.get("error"):
        raise PixivFieldError(f"{where} 回報錯誤：{payload.get('message')!r}")
    if payload.get("body") is None:
        raise PixivFieldError(f"{where} 缺少 'body' —— pixiv 可能改版了")
    return payload["body"]


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # pixiv 回 ISO8601 帶時區偏移（2023-01-01T00:00:00+09:00）
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def derive_page_url(original: str, index: int) -> str:
    """從第 0 頁的原圖網址推導第 N 頁。

    PBD 的作法（`Store.ts` 的 `addResult()`）：`_p0` → `_pN` 字串取代。
    一個作品不論幾頁都只花一次詳情請求 —— 在 1.8 秒間隔下，
    3000 個作品省下的是 90 分鐘。

    ⚠️ **推導不出來就丟例外，不猜。** 回一個「可能對」的網址，
    等於把「pximg 換了 URL 樣式」這件事變成幾百個看不出原因的 404。
    """
    if index == 0:
        return original
    new_url, count = _P0_SUFFIX.subn(f"_p{index}", original)
    if count != 1:
        raise PixivFieldError(
            f"無法從 {original!r} 推導第 {index} 頁的網址 —— "
            "pximg 的 URL 樣式可能改了，請重新確認 recon 筆記"
        )
    return new_url


class PixivAdapter:
    platform = "pixiv"

    # PBD 的實測經驗是 429 等 200 秒重試通常會成功（有帳號要重試 6 次）。
    # 但我們**不抄它的無限重試** —— 對一個會長時間跑的背景佇列，
    # 「無限次等 200 秒」的使用者體感是佇列卡死，不是錯誤。上限 2 次。
    # ⚠️ `download_delay_seconds=0` 是**有證據的**，不是為了快而放寬：
    # PixivBatchDownloader 的慢速抓取（slowCrawlDealy 1800ms）只套用在
    # `getWorksData()`（作品資料 API），下載端是 6 條並行、做完一個立刻補
    # 下一個，中間零延遲。我們的列舉端節流另外算（見 `fetch` 那一路），
    # 這裡只管 `i.pximg.net` 的檔案下載。
    rate_limit_policy = RateLimitPolicy(
        max_retries=2, wait_seconds=200.0, download_delay_seconds=0.0)

    # `www.pixiv.net` 的 ajax API 在 Cloudflare 後面，要瀏覽器 UA + Chrome
    # 的 cipher 順序才進得去（見檔案上方的實測表格）。
    # ⚠️ 這**只套用在平台 API**；CDN（`i.pximg.net`）走 `download_headers()`，
    # 那邊維持誠實的 `SNSMediaDL/0.1`，實測 200。
    client_profile = PIXIV_CLIENT_PROFILE

    def __init__(self, detail_delay: float = DETAIL_DELAY_SECONDS) -> None:
        # 間隔是**建構參數**不是全域設定：測試要能把它設成 0，
        # 否則跑一次測試要等好幾秒。
        self.detail_delay = detail_delay

    # ── PlatformAdapter ──────────────────────────────────

    def normalize(self, payload: Any) -> list[NormalizedPost]:
        """pixiv 沒有 extension 推送的路徑，資料一律由本 adapter 自己抓。

        介面要求要有這個方法，但走到這裡代表有人把 pixiv 的資料丟進
        `/api/ingest` —— 那是錯的，明講比默默回空清單好。
        """
        raise NotImplementedError(
            "pixiv 的資料由 backend 自己抓（POST /api/fetch），不走 ingest"
        )

    def download_headers(self, url: str) -> dict[str, str]:
        # i.pximg.net 沒有 Referer 會 403。**刻意不帶 cookie** ——
        # CDN 不需要憑證，憑證只活在列舉階段。
        return {"User-Agent": "SNSMediaDL/0.1", "Referer": REFERER}

    def auth_headers(self, cfg: Any, host: str) -> dict[str, str]:
        session_id = (getattr(cfg, "platform_credentials", None) or {}).get("pixiv")
        if not session_id:
            raise PixivFieldError(
                "pixiv 需要登入 cookie，但 platform_credentials 裡沒有 'pixiv'。"
                "請在 config.toml 設 platform_credentials = { pixiv = \"<PHPSESSID>\" }"
                "或設環境變數 SNSMEDIADL_PLATFORM_CREDENTIALS=pixiv=<PHPSESSID>"
            )
        return {"Cookie": f"PHPSESSID={session_id}", "Referer": REFERER}

    # ── IdListSource ─────────────────────────────────────

    async def resolve_account(
        self, client: httpx.AsyncClient, host: str, acct: str
    ) -> RemoteAccount:
        """pixiv 沒有穩定的 @handle，只吃數字 user id。

        使用者名稱可以隨時改，而且不唯一 —— 用它當身分會抓錯人。
        """
        user_id = acct.lstrip("@").strip()
        if not user_id.isdigit():
            raise PixivFieldError(
                f"pixiv 要數字 user id，收到 {acct!r}。"
                "它在個人頁網址裡：https://www.pixiv.net/users/12345"
            )

        r = await client.get(f"{API_ROOT}/ajax/user/{user_id}", params={"full": "0"})
        raise_if_not_found(r, f"ajax/user/{user_id}")
        r.raise_for_status()
        body = _body(r.json(), f"ajax/user/{user_id}")
        return RemoteAccount(
            platform_user_id=str(_need(body, "userId", "ajax/user")),
            screen_name=str(_need(body, "name", "ajax/user")),
            # 單一站台平台留空字串，不可以是 None（SQLite 唯一索引的 NULL 陷阱）
            instance_host="",
        )

    async def resolve_account_by_id(
        self, client: httpx.AsyncClient, host: str, user_id: str
    ) -> RemoteAccount:
        """pixiv 的 acct 本來就是穩定的數字 id —— 兩條路是同一條。

        （其他平台的 acct 是會改的 handle，所以那邊真的需要兩條。）
        """
        return await self.resolve_account(client, host, user_id)

    async def list_work_ids(
        self, client: httpx.AsyncClient, account: RemoteAccount
    ) -> list[str]:
        r = await client.get(
            f"{API_ROOT}/ajax/user/{account.platform_user_id}/profile/all"
        )
        # 帳號可能在「解析」與「列舉」之間被刪掉，而且更新既有帳號時
        # 走的是 resolve_account_by_id → 這裡，兩個端點都要判。
        raise_if_not_found(r, "profile/all")
        r.raise_for_status()
        body = _body(r.json(), "profile/all")

        ids: set[str] = set()
        for key in ("illusts", "manga"):
            if key not in body:
                raise PixivFieldError(
                    f"profile/all 缺少 {key!r} —— pixiv 可能改版了"
                )
            section = body[key]
            # 有作品時是 {id: null} 的字典；一件都沒有時 pixiv 回空陣列。
            # 這是 pixiv 真實存在的形狀差異，不是我在兜底。
            if isinstance(section, dict):
                ids.update(str(k) for k in section)
            elif isinstance(section, list):
                ids.update(str(v) for v in section)
            else:
                raise PixivFieldError(
                    f"profile/all 的 {key!r} 形狀不認得："
                    f"{type(section).__name__} —— pixiv 可能改版了"
                )

        non_numeric = [i for i in ids if not i.isdigit()]
        if non_numeric:
            raise PixivFieldError(
                f"profile/all 出現非數字的作品 id：{non_numeric[:3]} —— pixiv 可能改版了"
            )
        # 新到舊：id 越大越新
        return sorted(ids, key=int, reverse=True)

    def estimate_seconds(self, work_count: int) -> float:
        if work_count <= 0:
            return 0.0
        # 第一個請求不等待，之後每個等 detail_delay。
        # 動圖會多一個 ugoira_meta 請求，但事前不知道有幾個動圖，
        # 所以這是**下限**估計 —— 回報時要講清楚是「至少」。
        return (work_count - 1) * self.detail_delay

    async def fetch_works(
        self, client: httpx.AsyncClient, account: RemoteAccount, ids: list[str]
    ) -> list[NormalizedPost]:
        """逐一取詳情。**併發 1**（就是這個 for 迴圈）＋ 間隔 1.8 秒。

        PBD 的慢速模式是兩件事一起做：`ajaxThread = 1` 加上 sleep。
        只做 sleep 不做併發，實際速率會是預期的 N 倍。
        """
        posts: list[NormalizedPost] = []
        first = True
        for work_id in ids:
            if not first:
                await asyncio.sleep(self.detail_delay)
            first = False

            r = await client.get(
                f"{API_ROOT}/ajax/illust/{work_id}",
                # PBD 帶 time= 破快取。照做，成本是零。
                params={"time": str(int(datetime.now().timestamp() * 1000))},
            )
            r.raise_for_status()
            body = _body(r.json(), f"ajax/illust/{work_id}")
            posts.append(await self._to_post(client, account, str(work_id), body))
        return posts

    async def _to_post(
        self,
        client: httpx.AsyncClient,
        account: RemoteAccount,
        work_id: str,
        body: dict,
    ) -> NormalizedPost:
        where = f"ajax/illust/{work_id}"
        illust_type = int(_need(body, "illustType", where))
        x_restrict = int(_need(body, "xRestrict", where))

        if illust_type == ILLUST_TYPE_UGOIRA:
            media = [await self._ugoira_media(client, work_id, body)]
        else:
            page_count = int(_need(body, "pageCount", where))
            if page_count < 1:
                raise PixivFieldError(f"{where} 的 pageCount 是 {page_count} —— 不合理")
            original = str(_need(_need(body, "urls", where), "original", f"{where}.urls"))
            media = [
                NormalizedMedia(
                    ordinal=n,
                    kind=MediaKind.PHOTO.value,
                    source_url=derive_page_url(original, n),
                    # pixiv 是第一個真的有穩定媒體鍵的平台
                    platform_media_key=f"{work_id}_p{n}",
                    meta={
                        "illust_type": illust_type,
                        "page_count": page_count,
                        # 標記這個網址是推導來的：出事時查得到是哪一批
                        "url_derived": n > 0,
                    },
                )
                for n in range(page_count)
            ]

        return NormalizedPost(
            platform_post_id=work_id,
            platform_user_id=str(_need(body, "userId", where)),
            instance_host="",
            posted_at=_parse_date(body.get("createDate")),
            # pixiv 沒有轉推的概念
            is_retweet=False,
            media=media,
            # xRestrict 0=全年齡 1=R-18 2=R-18G。比 X 的 possibly_sensitive 強得多，
            # 但**仍然只當 auto 猜測**，不當權威分級（權威值只認人工標記）。
            sensitive_hint=True if x_restrict > 0 else None,
        )

    async def _ugoira_media(
        self, client: httpx.AsyncClient, work_id: str, body: dict
    ) -> NormalizedMedia:
        """動圖：多打一個 ugoira_meta 拿 zip 網址與每幀延遲。

        ⚠️ **抓不到要炸**，不可以當成「這個作品沒有媒體」跳過 ——
        那正是本專案定義的靜默漏抓。
        """
        await asyncio.sleep(self.detail_delay)
        r = await client.get(f"{API_ROOT}/ajax/illust/{work_id}/ugoira_meta")
        r.raise_for_status()
        meta = _body(r.json(), f"ugoira_meta/{work_id}")

        return NormalizedMedia(
            ordinal=0,
            kind=MediaKind.UGOIRA.value,
            source_url=str(_need(meta, "originalSrc", f"ugoira_meta/{work_id}")),
            platform_media_key=f"{work_id}_ugoira",
            meta={
                "illust_type": ILLUST_TYPE_UGOIRA,
                # 每幀的檔名與延遲毫秒數。第一版不轉檔，但幀資料要留 ——
                # 之後要轉的時候不必重抓。
                "frames": _need(meta, "frames", f"ugoira_meta/{work_id}"),
                "mime_type": meta.get("mime_type"),
                "page_count": body.get("pageCount"),
                "url_derived": False,
            },
        )

    # ── MediaUrlRepair ───────────────────────────────────

    async def repair_media_url(
        self, client: httpx.AsyncClient, platform_media_key: str, source_url: str
    ) -> str:
        """推導失效時的校正：問 `/ajax/illust/{id}/pages` 拿真正的網址。

        ⚠️ 這**不是**兜底。呼叫端要把它記成 WARNING ——
        走到這裡代表 pximg 的 URL 樣式變了，是需要有人知道的事實。
        """
        work_id, sep, page = platform_media_key.partition("_p")
        if not sep or not page.isdigit():
            raise PixivFieldError(
                f"{platform_media_key!r} 不是可校正的 pixiv 媒體鍵（要 <id>_p<n>）"
            )
        index = int(page)

        r = await client.get(f"{API_ROOT}/ajax/illust/{work_id}/pages")
        r.raise_for_status()
        pages = _body(r.json(), f"illust/{work_id}/pages")
        if not isinstance(pages, list):
            raise PixivFieldError(f"illust/{work_id}/pages 沒有回陣列 —— pixiv 可能改版了")
        if index >= len(pages):
            raise PixivFieldError(
                f"illust/{work_id}/pages 只有 {len(pages)} 頁，但要第 {index} 頁"
            )
        return str(
            _need(
                _need(pages[index], "urls", f"illust/{work_id}/pages[{index}]"),
                "original",
                f"illust/{work_id}/pages[{index}].urls",
            )
        )
