"""accounts 加 resume_cursor / resume_cursor_at（續抓點）

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-19

「撞到頁數上限之後再跑一次」在增量語意下是**無效**的：增量的停止條件是
「這一頁出現了已知的貼文」，而第 1 頁全是剛抓進來的 —— 立刻停，永遠到不了
第 21 頁。改跑 full 也沒用，那個迴圈一樣只跑 `fetch_max_pages` 頁。

所以「繼續抓」需要把下一頁的游標存起來。寫入點只有一處：
`services/fetch.py` 的游標迴圈結束時（撞上限就存，跟上了就清成 NULL）。

**不需要回填**：NULL 的語意就是「沒有續抓點」，對既有的 4,653 列全部成立。
既有帳號要有續抓點，得等它下一次真的撞到上限 —— 那是對的，因為我們並不
知道它們上次停在哪裡，憑空編一個游標比沒有更糟。

純新增欄位，走原生 `ALTER TABLE ADD COLUMN`，不 batch 重建 ——
理由見 `d4e5f6a7b8c9` 的 docstring（`op.add_column` 會靜默丟掉 CHECK；
batch 重建在 `PRAGMA foreign_keys=ON` 下有 cascade 刪資料的前科）。
"""
from __future__ import annotations

from alembic import op


revision = "f2a3b4c5d6e7"
down_revision = "e1f2a3b4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN resume_cursor TEXT")
    op.execute("ALTER TABLE accounts ADD COLUMN resume_cursor_at DATETIME")


def downgrade() -> None:
    # 沒有 CHECK 或索引參照這兩欄，DROP COLUMN 拿得掉，不必重建整張表。
    op.execute("ALTER TABLE accounts DROP COLUMN resume_cursor_at")
    op.execute("ALTER TABLE accounts DROP COLUMN resume_cursor")
