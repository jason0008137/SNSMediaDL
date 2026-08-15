"""accounts 的聚合欄去正規化 + media 的無選擇性索引改 partial

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-15

## 兩件事

### 1. accounts 加四個聚合欄並 backfill

`post_count` / `media_count` / `last_post_at` / `last_ingest_at`。
理由與實測數字見 `services/counters.py` 的 docstring。

四個都是純新增欄位，走原生 `ALTER TABLE ADD COLUMN`，不 batch 重建 ——
理由見 `d4e5f6a7b8c9` 的 docstring（`op.add_column` 會靜默丟掉 CHECK；
batch 重建在 `PRAGMA foreign_keys=ON` 下有 cascade 刪資料的前科）。

backfill 是一次 `UPDATE` 帶四個相關子查詢。在正式庫（4,653 帳號 /
163 萬貼文 / 224 萬媒體）上約需數秒 —— 它就是原本每次請求都在跑的那個查詢，
只是現在只跑一次。

### 2. 索引調整

- `ix_media_stars` → **完整** `(stars, id)`（原本是 `(stars)`）
- `ix_media_hash` → **partial** `WHERE file_hash IS NOT NULL`

⚠️⚠️ **`ix_media_stars` 一度被改成 partial `WHERE stars IS NOT NULL`，
那是錯的，實測抓到才改回來。** 記在這裡免得日後有人「順手最佳化」再犯一次：

正式庫的 `media.stars` **224 萬列全是 NULL**，所以那個 partial index 是**空的**。
`ORDER BY stars DESC, id DESC` 因此完全沒有索引可用，只能全表排序：

| 索引形狀 | ORDER BY stars | stars >= 3 |
|---------|---------------:|-----------:|
| partial `(stars,id) WHERE stars IS NOT NULL` | **2,140 ms** | 0 ms |
| 完整 `(stars, id)` | **1 ms** | 0 ms |
| 完整 `(stars)`（改動前） | 1 ms | 0 ms |

教訓：partial index 省下的是空間，但**排序需要的是完整的鍵序列**。
一個「大部分是 NULL」的欄位，正是 ORDER BY 最依賴索引的情況。

`file_hash` 沒有這個問題 —— 沒有任何查詢對它做 ORDER BY，只有等值比對，
而 partial index 對 `file_hash = ?` 實測仍會被選用。

⚠️ **`ix_media_status` 刻意不改。** 實測（SQLite 3.40.1）：partial index
`WHERE status != 'done'` 只在查詢**逐字寫成** `status != 'done'` 時才會被選用；
`status = 'pending'`（下載 worker 撿件用的）會退回 `SCAN media` 全表掃描。
改了等於修好佇列統計、弄壞下載 worker。
"""
from __future__ import annotations

from alembic import op


revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


# 四個聚合欄的定義。**必須與 `services/counters.py::_exprs` 等價** ——
# 兩邊算出不同的值，症狀是 `recount-accounts --check` 在剛 migrate 完的乾淨庫上
# 就報一堆不一致，而那看起來像維護路徑壞了。
_BACKFILL = """
UPDATE accounts SET
  post_count = (
    SELECT count(*) FROM posts WHERE posts.account_id = accounts.id),
  media_count = (
    SELECT count(*) FROM media JOIN posts ON media.post_id = posts.id
     WHERE posts.account_id = accounts.id),
  last_post_at = (
    SELECT max(posted_at) FROM posts WHERE posts.account_id = accounts.id),
  last_ingest_at = (
    SELECT max(ingested_at) FROM posts WHERE posts.account_id = accounts.id)
"""


def upgrade() -> None:
    # NOT NULL DEFAULT <常數> 在 SQLite 的 ADD COLUMN 是允許的
    op.execute("ALTER TABLE accounts ADD COLUMN post_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE accounts ADD COLUMN media_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE accounts ADD COLUMN last_post_at DATETIME")
    op.execute("ALTER TABLE accounts ADD COLUMN last_ingest_at DATETIME")
    op.execute(_BACKFILL)

    # ⚠️ 完整索引，**不是** partial。理由與實測數字見上方 docstring。
    op.execute("DROP INDEX IF EXISTS ix_media_stars")
    op.execute("CREATE INDEX ix_media_stars ON media (stars, id)")
    op.execute("DROP INDEX IF EXISTS ix_media_hash")
    op.execute(
        "CREATE INDEX ix_media_hash ON media (file_hash) WHERE file_hash IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_media_hash")
    op.execute("CREATE INDEX ix_media_hash ON media (file_hash)")
    op.execute("DROP INDEX IF EXISTS ix_media_stars")
    op.execute("CREATE INDEX ix_media_stars ON media (stars)")

    # 這四個欄位沒有 CHECK 參照它們，所以 DROP COLUMN 拿得掉，
    # 不必像 `e5f6a7b8c9d0` 那樣重建整張表。
    for col in ("last_ingest_at", "last_post_at", "media_count", "post_count"):
        op.execute(f"ALTER TABLE accounts DROP COLUMN {col}")
