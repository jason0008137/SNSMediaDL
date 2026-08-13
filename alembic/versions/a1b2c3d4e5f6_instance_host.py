"""add instance_host for fediverse platforms

Revision ID: a1b2c3d4e5f6
Revises: 3fa63fbe0dd9
Create Date: 2026-08-12

Fediverse 的同一個平台跑在很多站上（misskey.io / misskey.design、
baraag.net / pawoo.net...），不同 instance 的 user id 與 post id 都會撞。

instance_host 是 NOT NULL DEFAULT ''，不是 nullable ——
SQLite 的唯一索引把每個 NULL 都當成不同的值，含 NULL 的唯一鍵形同虛設。
既有的 X 資料填空字串。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "3fa63fbe0dd9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table：SQLite 不支援 ALTER 既有的 constraint，
    # alembic 會建新表、搬資料、換名。
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(
            sa.Column(
                "instance_host", sa.String(length=128),
                nullable=False, server_default="",
            )
        )
        batch.drop_constraint("uq_account_platform_user", type_="unique")
        batch.create_unique_constraint(
            "uq_account_platform_user",
            ["platform", "instance_host", "platform_user_id"],
        )

    with op.batch_alter_table("posts") as batch:
        batch.add_column(
            sa.Column(
                "instance_host", sa.String(length=128),
                nullable=False, server_default="",
            )
        )
        batch.drop_constraint("uq_post_platform_id", type_="unique")
        batch.create_unique_constraint(
            "uq_post_platform_id",
            ["platform", "instance_host", "platform_post_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("posts") as batch:
        batch.drop_constraint("uq_post_platform_id", type_="unique")
        batch.create_unique_constraint(
            "uq_post_platform_id", ["platform", "platform_post_id"]
        )
        batch.drop_column("instance_host")

    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("uq_account_platform_user", type_="unique")
        batch.create_unique_constraint(
            "uq_account_platform_user", ["platform", "platform_user_id"]
        )
        batch.drop_column("instance_host")
