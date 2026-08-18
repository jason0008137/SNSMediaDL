"""採集結果入庫。

**增量是預設行為，不是選項。** 已存在的貼文整則跳過，不更新、不重排隊。
要強制重抓走 `POST /api/media/{id}/retry`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import NormalizedPost, get_adapter
from ..db.enums import MediaStatus, Rating, RatingSource
from ..db.models import Account, Media, Post
from . import counters, identity


@dataclass
class IngestResult:
    posts_new: int = 0
    posts_skipped: int = 0
    media_new: int = 0
    account_id: int | None = None
    # 這一批順手把哪些「只有名字」的匯入帳號補上了真實 id。
    # ⚠️ 一定要帶出去給使用者看：這是**改動歷史資料歸屬**的操作，
    # 只寫進 log 的話，等於做了一件很大的事而當事人不知道。
    healed: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "posts_new": self.posts_new,
            "posts_skipped": self.posts_skipped,
            "media_new": self.media_new,
            "account_id": self.account_id,
            "healed": self.healed,
        }


# ── extension 送進來的最後一筆 ──────────────────────────
#
# GUI 把背景活動分成三條互不相干的流程（下載 worker / 抓取佇列 / extension
# 採集），第三條就是這裡。它要能說出自己的狀態，而在這之前**完全沒有任何
# 地方記錄過它**：使用者無從分辨「extension 沒在送」與「送了但 backend 沒收到」。
#
# ⚠️ 只在記憶體裡，重啟就沒了 —— 與抓取佇列同樣是 volatile。介面必須講明
# 這是「自 backend 啟動以來」，不可以在重啟後顯示成「從來沒有」。
_last_ingest: dict[str, Any] | None = None


def record_ingest(platform: str, screen_name: str | None, result: IngestResult) -> None:
    """記下這一次採集。由 `POST /api/ingest`（extension 的入口）呼叫。"""
    global _last_ingest
    _last_ingest = {
        "at": datetime.now(timezone.utc).isoformat(),
        "platform": platform,
        "screen_name": screen_name,
        "posts_new": result.posts_new,
        "media_new": result.media_new,
    }


def last_ingest() -> dict[str, Any] | None:
    """自 backend 啟動以來，extension 最後一次送進來的東西。沒有就是 None。"""
    return _last_ingest


def reset_last_ingest() -> None:
    """清掉那筆記錄。

    給測試用：它是**行程層級**的狀態（與抓取佇列、`runner.last_run()` 一樣），
    不歸任何一個 session 管，所以會跨測試殘留 —— 前一個測試 ingest 過，
    下一個測試就看不到「還沒收到過」這個初始狀態，而那正是介面要講的話。
    """
    global _last_ingest
    _last_ingest = None


def resolve_rating(
    post: NormalizedPost, account: Account
) -> tuple[str | None, str | None, str | None]:
    """決定 (rating, content_type, rating_source)。

    優先序（第一個命中就停）：
      1. 採集端明確帶的     -> manual
      2. 帳號預設值         -> account_default
      3. 平台的敏感旗標     -> auto
      4. 都沒有             -> 全部 NULL

    第 4 條刻意不猜 sfw：未知就是未知，猜錯的方向不對稱。
    """
    if post.rating or post.content_type:
        return post.rating, post.content_type, RatingSource.MANUAL.value

    if account.default_rating or account.default_content_type:
        return (
            account.default_rating,
            account.default_content_type,
            RatingSource.ACCOUNT_DEFAULT.value,
        )

    if post.sensitive_hint:
        return Rating.R18.value, None, RatingSource.AUTO.value

    return None, None, None


def upsert_account(
    session: Session,
    platform: str,
    platform_user_id: str,
    screen_name: str | None,
    instance_host: str = "",
    healed: list[dict[str, Any]] | None = None,
) -> Account:
    """找到或建立帳號。`healed` 有給的話，治療過的帳號會 append 一筆進去。"""
    healed_note: dict[str, Any] | None = None
    account = session.scalar(
        select(Account).where(
            Account.platform == platform,
            Account.instance_host == instance_host,
            Account.platform_user_id == platform_user_id,
        )
    )
    if account is None and screen_name:
        # ⚠️ **新建之前先看看有沒有「只有名字」的那一列。**
        #
        # 匯入的帳號 `platform_user_id` 是 `sn:<screen_name>` 哨符（沒有真 id）。
        # 直接新建一列的話，同一個人會變成兩列：一列有匯入的歷史、一列有之後
        # 採集的新東西，而帳號頁只會顯示兩張同名卡片，沒有任何提示。
        #
        # X 的真實 id **只有採集當下拿得到**（公開 API 已關），所以這件事沒辦法
        # 做成離線指令，只能在這條主路徑上做。
        #
        # 判斷依據是 screen_name，而 handle 會被釋出再被別人註冊 ——
        # 極少數情況下會歸錯戶。取捨與配套見 `services/identity.py` 與
        # `identity_heals` 表；那張表是事後回溯的唯一線索。
        account = identity.heal_placeholder_account(
            session, platform, instance_host,
            screen_name=screen_name, real_id=platform_user_id,
        )
        if account is not None:
            healed_note = {
                "screen_name": screen_name,
                "real_id": platform_user_id,
                "posts": account.post_count or 0,
                "media": account.media_count or 0,
            }

    if account is None:
        account = Account(
            platform=platform,
            instance_host=instance_host,
            platform_user_id=platform_user_id,
            screen_name=screen_name,
        )
        session.add(account)
        session.flush()
    elif screen_name and account.screen_name != screen_name:
        # 帳號改名是常態，更新顯示名稱；platform_user_id 才是身分。
        account.screen_name = screen_name
    if healed_note is not None and healed is not None:
        healed.append(healed_note)
    return account


def ingest(
    session: Session,
    platform: str,
    payload: Any,
    screen_name: str | None = None,
) -> IngestResult:
    """從原始 payload 入庫。extension 推進來的資料走這條。"""
    adapter = get_adapter(platform)
    return ingest_posts(session, platform, adapter.normalize(payload), screen_name)


def ingest_posts(
    session: Session,
    platform: str,
    posts: list[NormalizedPost],
    screen_name: str | None = None,
) -> IngestResult:
    """從**已正規化**的貼文入庫。

    backend 自己抓的平台（Misskey / Mastodon）走這條 —— 它們的 adapter 在
    `fetch_page()` 裡就把回應正規化好了，沒必要再倒回原始格式走一次。
    """
    result = IngestResult()

    if not posts:
        return result

    # screen_name 是整個 request 共用的，但一批貼文可能來自多個帳號。
    # 照套下去會把後面帳號的名字寫成第一個帳號的 —— 那是寫壞資料，不只是顯示錯。
    # 契約定為：screen_name 只在「整批同一個帳號」時採用。
    distinct_users = {np.platform_user_id for np in posts}
    effective_name = screen_name if len(distinct_users) == 1 else None

    accounts: dict[tuple[str, str], Account] = {}

    for np in posts:
        # 同一個 user id 在不同 instance 上是不同的人 —— key 一定要含 host
        key = (np.instance_host, np.platform_user_id)
        account = accounts.get(key)
        if account is None:
            account = upsert_account(
                session, platform, np.platform_user_id, effective_name,
                instance_host=np.instance_host, healed=result.healed,
            )
            accounts[key] = account
        result.account_id = account.id

        exists = session.scalar(
            select(Post.id).where(
                Post.platform == platform,
                Post.instance_host == np.instance_host,
                Post.platform_post_id == np.platform_post_id,
            )
        )
        if exists is not None:
            result.posts_skipped += 1
            continue

        rating, content_type, source = resolve_rating(np, account)
        post = Post(
            platform=platform,
            instance_host=np.instance_host,
            platform_post_id=np.platform_post_id,
            account_id=account.id,
            posted_at=np.posted_at,
            is_retweet=np.is_retweet,
            rating=rating,
            content_type=content_type,
            rating_source=source,
        )
        session.add(post)
        session.flush()
        result.posts_new += 1

        for nm in np.media:
            session.add(
                Media(
                    post_id=post.id,
                    ordinal=nm.ordinal,
                    kind=nm.kind,
                    platform_media_key=nm.platform_media_key,
                    source_url=nm.source_url,
                    status=MediaStatus.PENDING.value,
                    meta_json=json.dumps(nm.meta, ensure_ascii=False) if nm.meta else None,
                    # ⚠️ **這裡是 `media.posted_at` 的唯一寫入點。**
                    # 它是 `posts.posted_at` 的副本（見 db/models.py 的說明），
                    # 其他模組一律唯讀。多一個地方寫，兩表就會開始漂移。
                    posted_at=post.posted_at,
                )
            )
            result.media_new += 1

    # 聚合欄重算。**在 commit 之前**，跟貼文寫入同一個交易 ——
    # 分成兩個交易的話，中間 crash 會留下「貼文進去了但計數沒跟上」的狀態，
    # 而那正是快取值失準最難查的成因。
    #
    # 重算而不是 `+= result.posts_new`：這批可能同時碰到多個帳號
    # （`distinct_users`），而且部分貼文會因去重被跳過。重算不必配對，
    # 算錯一次也會被下一次呼叫修好。
    counters.recompute(session, [a.id for a in accounts.values()])

    session.commit()
    return result
