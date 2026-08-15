"""accounts 記錄最後一次擷取的時間與結果

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-15

四個純新增欄位，所以走原生 `ALTER TABLE ADD COLUMN`，不 batch 重建 ——
理由與注意事項見 `d4e5f6a7b8c9` 的 docstring（`op.add_column` 會靜默丟掉
CHECK；batch 重建在 `PRAGMA foreign_keys=ON` 下有 cascade 刪資料的前科）。

CHECK 的文字必須與 `db/models.py::Account.last_fetch_status` 逐字相同：
開發機走 `create_all`、正式庫走 migration，兩邊長不一樣的話會在最不方便
的時候才發現。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

_STATUS = (
    "last_fetch_status IS NULL OR last_fetch_status IN "
    "('ok', 'no_new', 'rate_limited', 'not_found', 'auth_required',"
    " 'failed', 'skipped')"
)

# 降級用。ADD COLUMN 加得進去，DROP COLUMN 卻拿不掉被 CHECK 參照的欄位，
# 所以降級只能重建整張表。
_ROLE = "role IS NULL OR role IN ('main', 'alt', 'r18_alt')"
_DEFAULT_RATING = "default_rating IS NULL OR default_rating IN ('sfw', 'r18')"
_DEFAULT_CONTENT = (
    "default_content_type IS NULL OR default_content_type IN "
    "('illust', 'irl', 'mod', 'ai', '3d', 'photograph', 'other')"
)
_STARS = "stars IS NULL OR (stars BETWEEN 1 AND 5)"


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
        sa.Column("is_favorite", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("stars", sa.Integer(), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("last_fetch_status", sa.String(length=16), nullable=True),
        sa.Column("last_fetch_note", sa.Text(), nullable=True),
        sa.Column("last_fetch_new_posts", sa.Integer(), nullable=True),
        sa.CheckConstraint(_DEFAULT_CONTENT, name="ck_default_content_type"),
        sa.CheckConstraint(_DEFAULT_RATING, name="ck_default_rating"),
        sa.CheckConstraint(_ROLE, name="ck_role"),
        sa.CheckConstraint(_STARS, name="ck_accounts_stars"),
        sa.CheckConstraint(_STATUS, name="ck_last_fetch_status"),
        sa.ForeignKeyConstraint(["creator_id"], ["creators.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform", "instance_host", "platform_user_id",
            name="uq_account_platform_user",
        ),
        sa.Index("ix_accounts_creator", "creator_id"),
    )


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN last_fetched_at DATETIME")
    op.execute(
        "ALTER TABLE accounts ADD COLUMN last_fetch_status VARCHAR(16) "
        f"CONSTRAINT ck_last_fetch_status CHECK ({_STATUS})"
    )
    op.execute("ALTER TABLE accounts ADD COLUMN last_fetch_note TEXT")
    op.execute("ALTER TABLE accounts ADD COLUMN last_fetch_new_posts INTEGER")


def downgrade() -> None:
    """刪欄位之前必須先刪它的 CHECK，順序不能反 —— batch 會照 copy_from
    重建整張表，把 CHECK 原封不動搬到已經沒有該欄位的新表上。"""
    with op.batch_alter_table(
        "accounts", copy_from=_accounts_with_new_columns()
    ) as batch:
        batch.drop_constraint("ck_last_fetch_status", type_="check")
        batch.drop_column("last_fetch_new_posts")
        batch.drop_column("last_fetch_note")
        batch.drop_column("last_fetch_status")
        batch.drop_column("last_fetched_at")
