"""五星評分（media / accounts）與帳號的我的最愛

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-14

⚠️ **本次刻意不用 batch 重建表。**

前三個 revision 都用 `batch_alter_table(copy_from=...)`，因為它們要改
**既有的 CHECK constraint** —— SQLite 沒有 ALTER CONSTRAINT，只能重建。
本次是**純新增欄位**，`ALTER TABLE ... ADD COLUMN` 就夠了。

不重建是有代價的差別：`a1b2c3d4e5f6` 就是 batch 重建時 `DROP TABLE` 在
`PRAGMA foreign_keys=ON` 下觸發 ON DELETE CASCADE，把 962 則貼文刪光，
而 migration 回報成功、沒有任何錯誤訊息。能不重建就不要重建。

用 `op.execute` 下原生 SQL 而不是 `op.add_column`：alembic 的 add_column
走 dialect 的 `get_column_specification`，那裡**不渲染 CHECK constraint** ——
寫 `sa.Column(..., sa.CheckConstraint(...))` 會被靜默丟掉，欄位加得成功、
約束不存在，而且沒有任何警告。這種漏法要等到有人存了 `stars=99` 才會發現。

SQLite 的 ADD COLUMN 允許行內 CHECK；既有列拿到 NULL，通過 `stars IS NULL`。
`is_favorite` 是 NOT NULL，所以必須給 server_default，否則既有列無值可填、
ALTER 直接失敗。

約束文字與 `db/models.py::_stars_check` 產生的必須逐字相同 —— 開發機走
`create_all`、正式庫走 migration，兩邊長不一樣的話會在最不方便的時候才發現。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

_STARS = "stars IS NULL OR (stars BETWEEN 1 AND 5)"

# 降級用。ADD COLUMN 加得進去，DROP COLUMN 卻拿不掉 —— SQLite 拒絕刪除
# 被 CHECK constraint 參照的欄位，所以降級只能重建整張表。
_ROLE = "role IS NULL OR role IN ('main', 'alt', 'r18_alt')"
_DEFAULT_RATING = "default_rating IS NULL OR default_rating IN ('sfw', 'r18')"
_DEFAULT_CONTENT = (
    "default_content_type IS NULL OR default_content_type IN "
    "('illust', 'irl', 'mod', 'ai', '3d', 'photograph', 'other')"
)
_KIND = "kind IN ('photo', 'video', 'animated_gif', 'ugoira')"
_STATUS = "status IN ('pending', 'downloading', 'done', 'failed')"


def _accounts_with_new_columns() -> sa.Table:
    """降級起點：**含**本 revision 新增欄位的 accounts。

    表定義寫死在這裡、不從 models import —— 日後 models 再改也不該回頭
    改變這個 revision 的行為。
    """
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
        sa.Column(
            "is_favorite", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("stars", sa.Integer(), nullable=True),
        sa.CheckConstraint(_DEFAULT_CONTENT, name="ck_default_content_type"),
        sa.CheckConstraint(_DEFAULT_RATING, name="ck_default_rating"),
        sa.CheckConstraint(_ROLE, name="ck_role"),
        sa.CheckConstraint(_STARS, name="ck_accounts_stars"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform", "instance_host", "platform_user_id",
            name="uq_account_platform_user",
        ),
        sa.Index("ix_accounts_creator", "creator_id"),
    )


def _media_with_new_columns() -> sa.Table:
    return sa.Table(
        "media",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("platform_media_key", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("file_hash", sa.String(length=64), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(), nullable=True),
        sa.Column("meta_json", sa.Text(), nullable=True),
        sa.Column("stars", sa.Integer(), nullable=True),
        sa.CheckConstraint(_KIND, name="ck_kind"),
        sa.CheckConstraint(_STATUS, name="ck_status"),
        sa.CheckConstraint(_STARS, name="ck_media_stars"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", "ordinal", name="uq_media_post_ordinal"),
        sa.Index("ix_media_status", "status"),
        sa.Index("ix_media_hash", "file_hash"),
        # ⚠️ 這裡**故意不宣告 `ix_media_stars`**，即使降級前它確實存在。
        # batch 把 copy_from 的索引全部重建到新表上，而新表已經沒有 stars 欄位
        # —— 宣告了就會炸在 `no such column: stars`。索引由 downgrade() 開頭
        # 顯式 drop。
    )


def upgrade() -> None:
    op.execute(
        "ALTER TABLE media ADD COLUMN stars INTEGER "
        f"CONSTRAINT ck_media_stars CHECK ({_STARS})"
    )
    op.execute(
        "ALTER TABLE accounts ADD COLUMN is_favorite BOOLEAN "
        "DEFAULT '0' NOT NULL"
    )
    op.execute(
        "ALTER TABLE accounts ADD COLUMN stars INTEGER "
        f"CONSTRAINT ck_accounts_stars CHECK ({_STARS})"
    )
    op.create_index("ix_media_stars", "media", ["stars"])


def downgrade() -> None:
    """⚠️ 刪欄位之前必須先刪它的 CHECK 與索引，順序不能反。

    `batch.drop_column("stars")` **不會**連帶拿掉 `ck_*_stars` ——
    batch 照著 `copy_from` 重建整張表，那個 CHECK 會被原封不動搬到
    已經沒有 `stars` 欄位的新表上，炸在 `no such column: stars`。
    索引同理（所以 `_media_with_new_columns()` 刻意不宣告 `ix_media_stars`）。
    """
    op.drop_index("ix_media_stars", table_name="media")
    with op.batch_alter_table("media", copy_from=_media_with_new_columns()) as batch:
        batch.drop_constraint("ck_media_stars", type_="check")
        batch.drop_column("stars")
    with op.batch_alter_table(
        "accounts", copy_from=_accounts_with_new_columns()
    ) as batch:
        batch.drop_constraint("ck_accounts_stars", type_="check")
        batch.drop_column("stars")
        batch.drop_column("is_favorite")
