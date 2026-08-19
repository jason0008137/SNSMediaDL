"""accounts 加 is_ignored（一鍵更新排除）

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-19

使用者標記「忽略」的帳號不列入一鍵更新（`plan_refresh()`）的目標。

**為什麼不沿用 `is_tracked`**：那一欄**也會被系統寫**（連續 2 次找不到就自動
退訂）。混用之後帳號頁就講不出「這是你標的」還是「系統放棄的」，而那兩者的
下一步不一樣。詳見 `db/models.py::Account.is_ignored` 的註解。

**不需要回填**：`False` 對既有的 4,653 列全部成立 —— 沒有任何帳號被使用者
標記過忽略，因為這個功能到現在才有。

純新增欄位，走原生 `ALTER TABLE ADD COLUMN`，不 batch 重建 ——
理由見 `d4e5f6a7b8c9` 的 docstring（`op.add_column` 會靜默丟掉 CHECK；
batch 重建在 `PRAGMA foreign_keys=ON` 下有 cascade 刪資料的前科）。
"""
from __future__ import annotations

from alembic import op


revision = "a3b4c5d6e7f8"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL DEFAULT <常數> 在 SQLite 的 ADD COLUMN 是允許的
    op.execute(
        "ALTER TABLE accounts ADD COLUMN is_ignored BOOLEAN NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    # 沒有 CHECK 或索引參照這一欄，DROP COLUMN 拿得掉，不必重建整張表。
    op.execute("ALTER TABLE accounts DROP COLUMN is_ignored")
