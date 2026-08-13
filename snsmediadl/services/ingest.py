"""採集結果入庫。

**增量是預設行為，不是選項。** 已存在的貼文整則跳過，不更新、不重排隊。
要強制重抓走 `POST /api/media/{id}/retry`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import NormalizedPost, get_adapter
from ..db.enums import MediaStatus, Rating, RatingSource
from ..db.models import Account, Media, Post


@dataclass
class IngestResult:
    posts_new: int = 0
    posts_skipped: int = 0
    media_new: int = 0
    account_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "posts_new": self.posts_new,
            "posts_skipped": self.posts_skipped,
            "media_new": self.media_new,
            "account_id": self.account_id,
        }


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
) -> Account:
    account = session.scalar(
        select(Account).where(
            Account.platform == platform,
            Account.instance_host == instance_host,
            Account.platform_user_id == platform_user_id,
        )
    )
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
                instance_host=np.instance_host,
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
                )
            )
            result.media_new += 1

    session.commit()
    return result
