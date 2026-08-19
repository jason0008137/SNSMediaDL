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

## ⚠️ 進度輸出涵蓋不到的兩段

分批 log 解決了「回填中沒有輸出」，但**沒有解決全部**。兩段仍然是靜默的，
而且都很長，所以 `upgrade()` 在進入它們之前各印一次預告：

1. **建索引** —— 要排序全表，224 萬列在外接碟上是數分鐘
2. **COMMIT + WAL checkpoint** —— 整張 media 被重寫過（ADD COLUMN + 224 萬列
   UPDATE）加一個新索引，全堆在 `-wal` 裡，可能好幾 GB 要寫回主檔

第 2 段發生在 `upgrade()` **回傳之後**，程式碼裡碰不到它 —— 唯一能做的就是
在返回前先把話講完。實測有使用者停在原本的「回填完成」那行問「該不該按叉叉」：
最後一個看得見的訊息寫著「完成」，而機器還要再忙十分鐘。
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
    #
    # ⚠️ 建索引要排序全表，224 萬列在外接碟上是數分鐘，而且**過程中沒有任何輸出**。
    # 所以前後各印一行：使用者要看得出「還在建」與「建完了」是兩個不同的狀態。
    log.info("建立索引 ix_media_posted（%s 列要排序，過程中不會有輸出）…", filled)
    op.execute("CREATE INDEX IF NOT EXISTS ix_media_posted ON media (posted_at, id)")
    log.info("索引建立完成")

    nulls = conn.exec_driver_sql(
        "SELECT COUNT(*) FROM media WHERE posted_at IS NULL").scalar()
    log.info("回填完成：%s 列已寫入，其中 %s 列的來源貼文本身沒有時間（NULL）",
             filled, nulls)

    # ⚠️⚠️ **這一段話不是客套，是這支 migration 唯一會被誤判成當機的地方。**
    #
    # upgrade() 到這裡就結束了，但**工作還沒結束**：接下來 alembic 要 COMMIT，
    # 而整張 media 被重寫過一遍（ADD COLUMN + 224 萬列 UPDATE）加上一個新索引，
    # 全部堆在 -wal 檔裡，commit 時要 fsync 再 checkpoint 回主檔 —— WAL 可能好幾 GB。
    #
    # 那一段完全沒有輸出，在外接碟上是數分鐘到十幾分鐘。實測有使用者在這裡
    # 停下來問「我該不該按叉叉」—— 因為上一行寫著「完成」。
    # 沒有這句話，最後一個看得到的訊息就是在騙人。
    log.info("接下來要把變更寫回磁碟（COMMIT + WAL checkpoint）。")
    log.info("⚠️ 這一步沒有進度輸出，224 萬列在外接碟上可能要數分鐘 —— 請不要中斷。")
    log.info("   想確認它還活著：看 DB 旁邊的 .db-wal 檔在縮小，或 .db 的修改時間在前進。")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_media_posted")
    op.execute("ALTER TABLE media DROP COLUMN posted_at")
