"""add 'ugoira' to media.kind

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-12

pixiv 的動圖是「一個 zip 裝一堆 jpg + 每幀的延遲毫秒數」，
既有的 photo / video / animated_gif 都不對。

⚠️ 這是 CHECK constraint 的變更，`Base.metadata.create_all()` **不會**幫既有的
DB 改（它只建不存在的表）。沒跑這個 migration 就寫 kind='ugoira'
會被 SQLite 擋下來。

⚠️ SQLite 不能 ALTER 既有 constraint，所以走 batch（建新表、搬資料、換名）。
`copy_from` 是必要的：SQLAlchemy 的 SQLite dialect **不反射 CHECK constraint**，
不明寫的話重建出來的表會連 ck_status 一起弄丟 —— 那是靜默的資料完整性損失。
**索引同理**：舊表被 drop 時索引跟著死，batch 只會重建 `copy_from` 上宣告過的，
漏寫就是靜默掉索引（schema 看起來對，只是查詢變慢）。
表定義在這裡寫死（不從 models import），這樣日後 models 再改也不會回頭改變
這個 revision 的行為。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

_KIND_OLD = "kind IN ('photo', 'video', 'animated_gif')"
_KIND_NEW = "kind IN ('photo', 'video', 'animated_gif', 'ugoira')"
_STATUS = "status IN ('pending', 'downloading', 'done', 'failed')"


def _media_table(kind_check: str) -> sa.Table:
    """media 表在本 revision 當下的樣子。"""
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
        sa.CheckConstraint(kind_check, name="ck_kind"),
        sa.CheckConstraint(_STATUS, name="ck_status"),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("post_id", "ordinal", name="uq_media_post_ordinal"),
        sa.Index("ix_media_status", "status"),
        sa.Index("ix_media_hash", "file_hash"),
    )


def upgrade() -> None:
    with op.batch_alter_table(
        "media", copy_from=_media_table(_KIND_OLD)
    ) as batch:
        batch.drop_constraint("ck_kind", type_="check")
        batch.create_check_constraint("ck_kind", _KIND_NEW)


def downgrade() -> None:
    # 有 ugoira 資料時不能降級：降下去那些列會違反 CHECK。
    # 明講而不是讓它在重建時失敗得莫名其妙。
    bind = op.get_bind()
    stuck = bind.execute(
        sa.text("SELECT COUNT(*) FROM media WHERE kind = 'ugoira'")
    ).scalar()
    if stuck:
        raise RuntimeError(
            f"有 {stuck} 筆 kind='ugoira' 的媒體，降級會讓它們違反 CHECK。"
            "請先處理這些資料（改 kind 或刪除）再降級。"
        )

    with op.batch_alter_table(
        "media", copy_from=_media_table(_KIND_NEW)
    ) as batch:
        batch.drop_constraint("ck_kind", type_="check")
        batch.create_check_constraint("ck_kind", _KIND_OLD)
