"""ORM models。

schema 的形狀是照著各平台 API 回應的實測結果設計的，不是先畫 ER 圖再套上去。
去識別化的 X GraphQL 回應樣本見 `extension/fixtures/x-usermedia-sample.json`。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .enums import (
    AccountRole,
    ContentType,
    MediaKind,
    MediaStatus,
    Rating,
    RatingSource,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _check(column: str, enum_cls, nullable: bool = True) -> CheckConstraint:
    """產生 `col IN (...)` 的 CHECK。SQLite 沒有原生 enum，靠這個守住值域。"""
    quoted = ", ".join(f"'{v}'" for v in enum_cls.values())
    expr = f"{column} IN ({quoted})"
    if nullable:
        expr = f"{column} IS NULL OR {expr}"
    return CheckConstraint(expr, name=f"ck_{column}")


def _stars_check(table: str) -> CheckConstraint:
    """五星評分的值域。NULL = 未評分，**不是** 0 分。

    ⚠️ 刻意放在 `mapped_column()` 的參數位置（欄位層）而不是 `__table_args__`
    （表層）：migration 用 `ALTER TABLE ... ADD COLUMN ... CHECK (...)` 加欄位，
    那是行內約束。兩邊要渲染出同一段 SQL，`create_all` 的開發機與跑過
    migration 的正式庫才不會長出不同的表。
    """
    return CheckConstraint(
        "stars IS NULL OR (stars BETWEEN 1 AND 5)", name=f"ck_{table}_stars"
    )


class Base(DeclarativeBase):
    pass


class Creator(Base):
    """一位創作者。底下可掛跨平台、跨帳號（含小帳）的多個 account。"""

    __tablename__ = "creators"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    accounts: Mapped[list[Account]] = relationship(back_populates="creator")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # 唯一鍵刻意「不含」creator_id：同一位 creator 可以在同一平台
        # 擁有多個帳號（本帳 + 小帳）。
        #
        # 含 instance_host：Fediverse 的同一個平台跑在很多站上
        # （misskey.io / misskey.design、baraag.net / pawoo.net…），
        # 不同 instance 的 user id 會撞。
        UniqueConstraint(
            "platform", "instance_host", "platform_user_id",
            name="uq_account_platform_user",
        ),
        Index("ix_accounts_creator", "creator_id"),
        _check("role", AccountRole),
        _check("default_rating", Rating),
        _check("default_content_type", ContentType),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    # 單一站台的平台（X、pixiv）留空字串。
    #
    # ⚠️ **不可以用 NULL 表示「沒有 instance」**：SQLite 的唯一索引把每個 NULL
    # 都當成不同的值，含 NULL 的唯一鍵形同虛設 —— X 的帳號會變成每次 ingest
    # 都新建一筆而不是去重，而且完全沒有錯誤訊息。
    instance_host: Mapped[str] = mapped_column(String(128), default="", server_default="")
    platform_user_id: Mapped[str] = mapped_column(String(64))
    screen_name: Mapped[str | None] = mapped_column(String(200), default=None)
    is_tracked: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    creator_id: Mapped[int | None] = mapped_column(
        ForeignKey("creators.id", ondelete="SET NULL"), default=None
    )
    role: Mapped[str | None] = mapped_column(String(16), default=None)

    # 帳號層預設值。ingest 時繼承給新貼文；改這裡不回溯既有貼文。
    default_rating: Mapped[str | None] = mapped_column(String(8), default=None)
    default_content_type: Mapped[str | None] = mapped_column(String(16), default=None)

    # 個人偏好。與 default_rating 無關 —— 分級是內容的性質、會繼承給貼文，
    # 這兩個純粹是「我多喜歡這個帳號」，不繼承、不回溯、沒有來源概念。
    is_favorite: Mapped[bool] = mapped_column(
        default=False, server_default="0", nullable=False
    )
    # NULL = 未評分。1–5。accounts 是小表，不加索引。
    stars: Mapped[int | None] = mapped_column(
        Integer, _stars_check("accounts"), default=None
    )

    # ── 最後一次擷取 ──
    # ⚠️ 記的是**嘗試**不是成功。記成功時間的話，一個連續失敗三個月的帳號
    # 會顯示「三個月前」，跟一個三個月沒查過的帳號無法區分 —— 而那正是
    # 這幾個欄位要分辨的兩件事。
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_fetch_status: Mapped[str | None] = mapped_column(
        String(16),
        CheckConstraint(
            "last_fetch_status IS NULL OR last_fetch_status IN "
            "('ok', 'no_new', 'rate_limited', 'not_found', 'auth_required',"
            " 'failed', 'skipped')",
            name="ck_last_fetch_status",
        ),
        default=None,
    )
    # stopped_because 或錯誤訊息原文。失敗的原因不記下來，帳號頁上就只剩
    # 一個「失敗」，使用者無從知道是改名還是憑證過期。
    last_fetch_note: Mapped[str | None] = mapped_column(Text, default=None)
    last_fetch_new_posts: Mapped[int | None] = mapped_column(Integer, default=None)

    # ── 聚合欄（去正規化的快取值）──
    #
    # ⚠️ 這四個是**算出來的**，不是輸入的。真值永遠在 posts / media 那邊。
    # 維護與檢查一律走 `services/counters.py`，不要在別處手動加減 ——
    # 理由與實測數字寫在那個模組的 docstring。
    #
    # `snsmediadl recount-accounts --check` 會比對它們與真值。
    post_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    media_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # 作者最新一則貼文的時間。與 last_fetched_at 是兩件事：那個是「我何時查的」。
    last_post_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 我最近一次**抓到新東西**的時間。查了但對方沒發新的不會動到它。
    last_ingest_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    creator: Mapped[Creator | None] = relationship(back_populates="accounts")
    posts: Mapped[list[Post]] = relationship(back_populates="account")


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        # 貼文層去重鍵：重跑同一帳號不會重複建 record。
        # 含 instance_host 的理由同 accounts —— 兩個 Mastodon 站各自產生的
        # snowflake id 會撞，撞到就是把別人的貼文當成同一則而靜默略過。
        UniqueConstraint(
            "platform", "instance_host", "platform_post_id",
            name="uq_post_platform_id",
        ),
        Index("ix_posts_rating", "rating"),
        Index("ix_posts_account", "account_id"),
        _check("rating", Rating),
        _check("content_type", ContentType),
        _check("rating_source", RatingSource),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    instance_host: Mapped[str] = mapped_column(String(128), default="", server_default="")
    platform_post_id: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))

    posted_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    is_retweet: Mapped[bool] = mapped_column(default=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # NULL = 未知。刻意不預設 sfw。
    rating: Mapped[str | None] = mapped_column(String(8), default=None)
    content_type: Mapped[str | None] = mapped_column(String(16), default=None)
    rating_source: Mapped[str | None] = mapped_column(String(16), default=None)

    account: Mapped[Account] = relationship(back_populates="posts")
    media: Mapped[list[Media]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class Media(Base):
    __tablename__ = "media"
    __table_args__ = (
        UniqueConstraint("post_id", "ordinal", name="uq_media_post_ordinal"),
        # ⚠️ 這三個索引的形狀是實測定下來的，改之前先讀
        # `alembic/versions/f6a7b8c9d0e1_account_counters.py` 的 docstring。
        #
        # · status 保持**完整**索引：partial `WHERE status != 'done'` 會讓下載
        #   worker 的 `status = 'pending'` 退回全表掃描（SQLite 不推論蘊含關係）
        # · stars 保持**完整** `(stars, id)`：正式庫 224 萬列全是 NULL，改 partial
        #   會讓索引變空，`ORDER BY stars` 從 1 ms 變成 2,140 ms
        # · file_hash 可以 partial：只有等值比對，沒有 ORDER BY
        Index("ix_media_status", "status"),
        Index("ix_media_hash", "file_hash",
              sqlite_where=text("file_hash IS NOT NULL")),
        Index("ix_media_stars", "stars", "id"),
        _check("kind", MediaKind, nullable=False),
        _check("status", MediaStatus, nullable=False),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)

    kind: Mapped[str] = mapped_column(String(16))
    # 平台的穩定識別碼。URL 會隨 CDN 變動，這個不會 —— 但不是每個平台都有。
    platform_media_key: Mapped[str | None] = mapped_column(String(64), default=None)
    # 只是「下載用的位址」，不當識別碼。
    source_url: Mapped[str] = mapped_column(Text, default="")

    local_path: Mapped[str | None] = mapped_column(Text, default=None)
    file_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    bytes: Mapped[int | None] = mapped_column(Integer, default=None)

    status: Mapped[str] = mapped_column(String(16), default=MediaStatus.PENDING.value)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    meta_json: Mapped[str | None] = mapped_column(Text, default=None)

    # 五星評分。掛在 media 不掛 post —— rating（sfw/r18）是**貼文內容**的性質，
    # 同一則的四張圖不會一張 sfw 一張 r18；stars 是**對單張圖的偏好**，
    # 同一則裡有張特別好是常態。GUI 的瀏覽單位也是 media。
    stars: Mapped[int | None] = mapped_column(
        Integer, _stars_check("media"), default=None
    )

    post: Mapped[Post] = relationship(back_populates="media")
