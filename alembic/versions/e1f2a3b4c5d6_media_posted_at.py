"""media 加 posted_at（posts.posted_at 的副本）+ 索引 + 分批回填

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-18

## 為什麼要反正規化

媒體頁要能依「推文時間」排序。時間在 `posts`、媒體在 `media`（正式庫 224 萬列），
join 之後在深頁排序必然慢，而且 keyset 分頁跨表寫起來很容易靜默跳筆。

代價寫在 `db/models.py::Media.posted_at`：它會漂移，防線是單一寫入點
（只有 `services/ingest.py` 寫）+ `snsmediadl check-posted-at` 可查。

## 索引形狀

`ix_media_posted (posted_at, id)` —— **完整**，不是 partial。
與 `ix_media_stars` 同一個教訓（見 `f6a7b8c9d0e1` 的 docstring）：
`WHERE posted_at IS NOT NULL` 的 partial index 會讓 56,631 筆 NULL 進不了索引，
而**分頁的第二段正好要掃它們**。

## 回填

⚠️ 224 萬列，使用者會自己在正式庫上跑，**進度要看得見**。
以 id 區間分批（每批 5 萬），每批 log 一次。整批一次 UPDATE 也跑得完，
但那是一個十幾分鐘沒有任何輸出的畫面 —— 使用者會以為當掉了而按 Ctrl-C。

索引在**回填之後**才建：先建索引等於每批 UPDATE 都要維護它。
"""
from __future__ import annotations

import logging

from alembic import op


revision = "e1f2a3b4c5d6"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

BATCH = 50_000


def upgrade() -> None:
    conn = op.get_bind()
    op.execute("ALTER TABLE media ADD COLUMN posted_at DATETIME")

    max_id = conn.exec_driver_sql("SELECT COALESCE(MAX(id), 0) FROM media").scalar()
    log.info("回填 media.posted_at：共 %s 列，每批 %s", max_id, BATCH)

    filled = 0
    start = 0
    while start <= max_id:
        end = start + BATCH
        result = conn.exec_driver_sql(
            "UPDATE media SET posted_at = ("
            "  SELECT posts.posted_at FROM posts WHERE posts.id = media.post_id)"
            f" WHERE media.id > {start} AND media.id <= {end}"
        )
        filled += result.rowcount or 0
        log.info("  已處理 id ≤ %s（累計 %s 列）", min(end, max_id), filled)
        start = end

    # 完整複合索引，**不是** partial —— 理由見上方 docstring。
    op.execute("CREATE INDEX IF NOT EXISTS ix_media_posted ON media (posted_at, id)")

    nulls = conn.exec_driver_sql(
        "SELECT COUNT(*) FROM media WHERE posted_at IS NULL").scalar()
    log.info("回填完成：%s 列已寫入，其中 %s 列的來源貼文本身沒有時間（NULL）",
             filled, nulls)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_media_posted")
    op.execute("ALTER TABLE media DROP COLUMN posted_at")
