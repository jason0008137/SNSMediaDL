"""平台無關的抓取服務。

給一個帳號 → 列舉 → 入庫 → 交給既有的下載佇列。
**adapter 負責「怎麼問平台」，這裡負責「問到什麼時候為止」。**

有兩種列舉形狀，依 adapter 的能力分流：

  - `SourceAdapter`  游標式分頁（Misskey / Mastodon）—— 抓下來才知道抓過了
  - `IdListSource`   一次拿全部 id（pixiv）—— **先知道，再決定抓不抓**

X 兩種都不是，資料來源是 extension，呼叫這裡會明確報錯。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..adapters import IdListSource, RemoteAccount, get_source_adapter
from ..config import Config
from ..db.models import Post
from .identity import heal_placeholder_account
from .ingest import ingest_posts

log = logging.getLogger("snsmediadl.fetch")

# SQLite 對單一 SQL 的變數個數有上限（歷史上 999）。
# pixiv 的 profile/all 可能一次回上萬個 id，不分批會直接爆。
_ID_CHUNK = 500


@dataclass
class FetchResult:
    account: str = ""
    instance_host: str = ""
    pages: int = 0
    posts_seen: int = 0
    posts_new: int = 0
    media_new: int = 0
    stopped_because: str = ""
    errors: list[str] = field(default_factory=list)

    # ── 只有 id 清單式平台（pixiv）會填 ──────────────────
    # 存在的理由：3000 個作品要跑 90 分鐘，使用者必須在按下去之前就知道。
    works_total: int = 0
    works_to_fetch: int = 0
    estimated_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "account": self.account,
            "instance_host": self.instance_host,
            "pages": self.pages,
            "posts_seen": self.posts_seen,
            "posts_new": self.posts_new,
            "media_new": self.media_new,
            "stopped_because": self.stopped_because,
            "errors": self.errors,
            "works_total": self.works_total,
            "works_to_fetch": self.works_to_fetch,
            "estimated_seconds": self.estimated_seconds,
        }


def _known_post_ids(
    session: Session, platform: str, instance_host: str, post_ids: list[str]
) -> set[str]:
    if not post_ids:
        return set()
    known: set[str] = set()
    # 分批：游標式一次只問 40 個沒差，pixiv 可能一次問上萬個。
    for start in range(0, len(post_ids), _ID_CHUNK):
        chunk = post_ids[start : start + _ID_CHUNK]
        rows = session.scalars(
            select(Post.platform_post_id).where(
                Post.platform == platform,
                Post.instance_host == instance_host,
                Post.platform_post_id.in_(chunk),
            )
        ).all()
        known.update(rows)
    return known


async def fetch_account(
    cfg: Config,
    maker: sessionmaker[Session],
    *,
    platform: str,
    host: str,
    acct: str,
    full: bool = False,
    user_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchResult:
    """抓一個帳號。

    `full=False`（預設）是**增量**：已經在 DB 的貼文不重抓。
    重跑不該重抓 —— 這是專案的預設行為，不是選項。

    `full=True` 不提早停（游標式）／不濾掉已知 id（清單式），
    用於「補抓中間漏掉的」。它仍然不會重複入庫（ingest 本身就去重）。

    `user_id` 有值時用**平台的 user id** 解析帳號，`acct` 只當顯示名。
    更新 DB 既有帳號一律帶它 —— 帳號改名是常態，拿舊名字去查會 404，
    那個帳號從此就再也更新不到（而且錯誤看起來只是「找不到」）。
    """
    adapter = get_source_adapter(platform)
    result = FetchResult(instance_host=host)

    headers = {"User-Agent": "SNSMediaDL/0.1"}
    # 認證方式是平台知識：Fediverse 是 Bearer，pixiv 是 Cookie。
    # 這一層不該認得任何一種。
    headers.update(adapter.auth_headers(cfg, host))

    async with httpx.AsyncClient(
        transport=transport, timeout=cfg.timeout_seconds, headers=headers
    ) as client:
        account: RemoteAccount = (
            await adapter.resolve_account_by_id(client, host, user_id)
            if user_id
            else await adapter.resolve_account(client, host, acct)
        )
        result.account = account.screen_name

        if user_id is None:
            # 走名字解析代表 DB 裡那一列可能還帶著 `sn:` 哨符（匯入來的帳號
            # 只有名字、沒有平台 id）。現在拿到真 id 了，**就地把那一列治好** ——
            # 不做的話下一步的 ingest 會用真 id 新建第二列，把同一個人的記錄
            # 劈成兩半：匯入來的貼文全留在哨符那一列，而畫面上看不出發生過這件事。
            def _heal() -> None:
                with maker() as session:
                    healed = heal_placeholder_account(
                        session, platform, host,
                        screen_name=account.screen_name,
                        real_id=account.platform_user_id,
                    )
                    if healed is not None:
                        session.commit()

            await asyncio.to_thread(_heal)

        if isinstance(adapter, IdListSource):
            await _fetch_by_id_list(
                cfg, maker, adapter, client, account,
                platform=platform, host=host, full=full, result=result,
            )
        else:
            cursor: str | None = None
            for page_no in range(1, cfg.fetch_max_pages + 1):
                page = await adapter.fetch_page(
                    client, account, cursor, cfg.fetch_page_size
                )
                result.pages = page_no
                result.posts_seen += len(page.posts)

                if page.posts:
                    def _work(session: Session) -> tuple[int, int, bool]:
                        ids = [p.platform_post_id for p in page.posts]
                        known = _known_post_ids(session, platform, host, ids)
                        r = ingest_posts(
                            session, platform, page.posts,
                            screen_name=account.screen_name,
                        )
                        return r.posts_new, r.media_new, bool(known)

                    def _run() -> tuple[int, int, bool]:
                        with maker() as session:
                            return _work(session)

                    posts_new, media_new, hit_known = await asyncio.to_thread(_run)
                    result.posts_new += posts_new
                    result.media_new += media_new

                    # 增量停止條件：這一頁出現了已知的貼文。
                    #
                    # ⚠️ 用「已知的貼文 id」而不是時間戳：置頂貼文會排在最前面、
                    # 編輯過的貼文會更新時間，兩者都會讓時間序不可靠。
                    if hit_known and not full:
                        result.stopped_because = "碰到已抓過的貼文（增量）"
                        break

                if not page.next_cursor:
                    result.stopped_because = "沒有下一頁了"
                    break
                cursor = page.next_cursor

                # 列舉的節流與下載分開：列舉打的是 API host，下載打的是媒體 CDN，
                # 兩者的限制不同，共用一個節流只會互相拖慢。
                await asyncio.sleep(cfg.fetch_delay_seconds)
            else:
                # for 沒有 break = 撞到頁數上限。這件事要說出來，
                # 否則使用者會以為「抓完了」，其實只是抓到上限。
                result.stopped_because = (
                    f"達到頁數上限 {cfg.fetch_max_pages} 頁 —— "
                    "可能還有更舊的內容，再跑一次或調高 fetch_max_pages"
                )

    log.info(
        "抓取 %s@%s：%s 頁 / 看到 %s 則 / 新增 %s 則 %s 個媒體（%s）",
        result.account, host, result.pages, result.posts_seen,
        result.posts_new, result.media_new, result.stopped_because,
    )
    return result


async def _fetch_by_id_list(
    cfg: Config,
    maker: sessionmaker[Session],
    adapter: IdListSource,
    client: httpx.AsyncClient,
    account: RemoteAccount,
    *,
    platform: str,
    host: str,
    full: bool,
    result: FetchResult,
) -> None:
    """id 清單式列舉（pixiv）。

    重點：**增量在發出任何詳情請求之前就完成**。
    `profile/all` 一個請求拿到全部 id，跟 DB 一比就知道要問哪些 ——
    在 1.8 秒一個請求的節流下，這個順序差幾十分鐘。
    """
    ids = await adapter.list_work_ids(client, account)
    result.works_total = len(ids)

    if full:
        todo = list(ids)
    else:
        def _filter() -> list[str]:
            with maker() as session:
                known = _known_post_ids(session, platform, host, ids)
            return [i for i in ids if i not in known]

        todo = await asyncio.to_thread(_filter)

    result.works_to_fetch = len(todo)
    result.estimated_seconds = adapter.estimate_seconds(len(todo))

    # 這行是「按下去之前就知道要等多久」的落地點。
    # 說「至少」是因為動圖會多打一個 ugoira_meta，事前不知道有幾個。
    log.info(
        "%s 共 %s 個作品，其中 %s 個沒抓過 —— 預估至少 %.1f 分鐘",
        account.screen_name, result.works_total, result.works_to_fetch,
        result.estimated_seconds / 60,
    )

    if not todo:
        result.stopped_because = "全部作品都抓過了（增量）"
        return

    # 分批入庫而不是全部抓完再入庫：3000 個作品要跑 90 分鐘，
    # 中途中斷不該把前面 80 分鐘的成果丟掉。
    for start in range(0, len(todo), cfg.fetch_batch_size):
        chunk = todo[start : start + cfg.fetch_batch_size]
        posts = await adapter.fetch_works(client, account, chunk)
        result.pages += 1
        result.posts_seen += len(posts)

        if not posts:
            continue

        def _run(batch=posts) -> tuple[int, int]:
            with maker() as session:
                r = ingest_posts(
                    session, platform, batch, screen_name=account.screen_name
                )
                return r.posts_new, r.media_new

        posts_new, media_new = await asyncio.to_thread(_run)
        result.posts_new += posts_new
        result.media_new += media_new

    result.stopped_because = "全部作品都處理完了"
