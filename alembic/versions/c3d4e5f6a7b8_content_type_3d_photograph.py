"""add '3d' and 'photograph' to content_type

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-12

`content_type` 的 CHECK 出現在**兩張表**：`posts.content_type` 與
`accounts.default_content_type`。只改一張的症狀是「貼文標得起來、
帳號預設值標不起來」—— 而且錯誤訊息是 SQLite 的 CHECK 失敗，
看不出是漏改了哪裡。

⚠️ 同 b2c3d4e5f6a7：SQLAlchemy 的 SQLite dialect **不反射 CHECK constraint**，
所以 batch 重建時必須用 `copy_from` 明寫整張表，否則同表的其他 CHECK
（ck_rating、ck_rating_source、ck_role…）會被靜默弄丟。
**索引也要一起明寫** —— 舊表 drop 時索引跟著死，batch 只重建 `copy_from`
上宣告過的（`tests/test_migration.py::test_alembic_matches_models` 會抓）。

表定義寫死在這裡、不從 models import：日後 models 再改也不該回頭改變
這個 revision 的行為。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None

_OLD = "('illust', 'irl', 'mod', 'ai', 'other')"
_NEW = "('illust', 'irl', 'mod', 'ai', '3d', 'photograph', 'other')"

_RATING = "rating IS NULL OR rating IN ('sfw', 'r18')"
_RATING_SOURCE = (
    "rating_source IS NULL OR "
    "rating_source IN ('manual', 'account_default', 'auto')"
)
_ROLE = "role IS NULL OR role IN ('main', 'alt', 'r18_alt')"
_DEFAULT_RATING = "default_rating IS NULL OR default_rating IN ('sfw', 'r18')"


def _posts(values: str) -> sa.Table:
    return sa.Table(
        "posts",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column(
            "instance_host", sa.String(length=128),
            nullable=False, server_default="",
        ),
        sa.Column("platform_post_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("is_retweet", sa.Boolean(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
        sa.Column("rating", sa.String(length=8), nullable=True),
        sa.Column("content_type", sa.String(length=16), nullable=True),
        sa.Column("rating_source", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            f"content_type IS NULL OR content_type IN {values}",
            name="ck_content_type",
        ),
        sa.CheckConstraint(_RATING, name="ck_rating"),
        sa.CheckConstraint(_RATING_SOURCE, name="ck_rating_source"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform", "instance_host", "platform_post_id",
            name="uq_post_platform_id",
        ),
        sa.Index("ix_posts_rating", "rating"),
        sa.Index("ix_posts_account", "account_id"),
    )


def _accounts(values: str) -> sa.Table:
    return sa.Table(
        "accounts",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column(
            "instance_host", sa.String(length=128),
            nullable=False, server_default="",
        ),
        sa.Column("platform_user_id", sa.String(length=64), nullable=False),
        sa.Column("screen_name", sa.String(length=200), nullable=True),
        sa.Column("is_tracked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("creator_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(length=16), nullable=True),
        sa.Column("default_rating", sa.String(length=8), nullable=True),
        sa.Column("default_content_type", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            f"default_content_type IS NULL OR default_content_type IN {values}",
            name="ck_default_content_type",
        ),
        sa.CheckConstraint(_DEFAULT_RATING, name="ck_default_rating"),
        sa.CheckConstraint(_ROLE, name="ck_role"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform", "instance_host", "platform_user_id",
            name="uq_account_platform_user",
        ),
        sa.Index("ix_accounts_creator", "creator_id"),
    )


def _swap(from_values: str, to_values: str) -> None:
    with op.batch_alter_table("posts", copy_from=_posts(from_values)) as batch:
        batch.drop_constraint("ck_content_type", type_="check")
        batch.create_check_constraint(
            "ck_content_type", f"content_type IS NULL OR content_type IN {to_values}"
        )

    with op.batch_alter_table("accounts", copy_from=_accounts(from_values)) as batch:
        batch.drop_constraint("ck_default_content_type", type_="check")
        batch.create_check_constraint(
            "ck_default_content_type",
            f"default_content_type IS NULL OR default_content_type IN {to_values}",
        )


def upgrade() -> None:
    _swap(_OLD, _NEW)


def downgrade() -> None:
    # 已經標了新標籤的資料降級會違反 CHECK。明講，不要讓它在重建時失敗得莫名其妙。
    bind = op.get_bind()
    for table, column in (("posts", "content_type"), ("accounts", "default_content_type")):
        stuck = bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table} WHERE {column} IN ('3d', 'photograph')"
            )
        ).scalar()
        if stuck:
            raise RuntimeError(
                f"{table}.{column} 有 {stuck} 筆是 '3d' 或 'photograph'，"
                "降級會讓它們違反 CHECK。請先改掉這些值再降級。"
            )

    _swap(_NEW, _OLD)
