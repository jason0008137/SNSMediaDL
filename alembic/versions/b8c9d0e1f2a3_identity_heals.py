"""identity_heals：帳號身分補齊的紀錄

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-16

匯入器對只有名字、沒有平台數字 id 的帳號寫 `sn:<screen_name>` 哨符。
真實 id 只有採集當下拿得到，所以治療發生在 ingest 主路徑上（見
`services/identity.py`）。這張表記下每一次治療。

⚠️ 為什麼要記：治療的判斷是「同平台同 screen_name」，而平台的 handle 會被
釋出再被別人註冊。極少數情況下會把舊媒體掛到新主人身上，那個錯誤無法完全
避免 —— 但它必須留下痕跡。log 會捲掉，這張表不會。

純新增一張表，不動既有資料。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identity_heals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("instance_host", sa.String(128), nullable=False, server_default=""),
        sa.Column("screen_name", sa.String(200), nullable=False),
        sa.Column("placeholder_id", sa.String(64), nullable=False),
        sa.Column("real_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("moved_posts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("identity_heals")
