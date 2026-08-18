"""accounts 加 not_found_streak（連續「找不到」次數）

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-18

自動退訂（連續 2 次找不到就把 pixiv 帳號移出追蹤名單）要記連續次數。
理由與判定條件見 `db/models.py::Account.not_found_streak` 與
`adapters/pixiv.py::PixivNotFound`。

純新增欄位，走原生 `ALTER TABLE ADD COLUMN`，不 batch 重建 ——
理由見 `d4e5f6a7b8c9` 的 docstring（`op.add_column` 會靜默丟掉 CHECK；
batch 重建在 `PRAGMA foreign_keys=ON` 下有 cascade 刪資料的前科）。

既有列全部填 0：**不從 `last_fetch_status` 反推**。反推等於憑一次舊記錄
就給某些帳號記上一筆，而那筆 404 可能是幾個月前的別種原因 ——
下一次 404 就會直接觸發退訂。從 0 開始，最多晚一輪。
"""
from __future__ import annotations

from alembic import op


revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL DEFAULT <常數> 在 SQLite 的 ADD COLUMN 是允許的
    op.execute(
        "ALTER TABLE accounts ADD COLUMN not_found_streak INTEGER NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    # 沒有 CHECK 參照這個欄位，DROP COLUMN 拿得掉，不必重建整張表。
    op.execute("ALTER TABLE accounts DROP COLUMN not_found_streak")
