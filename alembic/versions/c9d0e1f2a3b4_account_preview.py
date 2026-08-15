"""accounts.preview_media：帳號卡的預覽縮圖

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-16

存最新 4 個 media id（JSON 陣列）。

## 為什麼要存起來

即席算「每個帳號最新 4 張」在正式庫（4,653 帳號 / 163 萬貼文 / 224 萬媒體）
上量過：

| 做法 | 耗時 |
|------|-----:|
| 逐帳號查，**帶** `kind='photo' AND status='done'` | **217,646 ms** |
| 逐帳號查，不帶那兩個條件 | 359 ms（一頁 100 個帳號）|
| 全表視窗函式 | **4,210 ms**（4,649 個帳號一次算完）|

第一列與第二列的差別只有兩個 WHERE 條件：加了它們之後 SQLite 改從
`ix_media_status` 驅動，掃 224 萬列。這就是為什麼 `counters._PREVIEW_SQL`
刻意不濾 kind 與 status —— 影片沒有縮圖是**前端**用 ▶ 佔位解決的。

## backfill 用視窗函式，不逐帳號跑

4,653 個帳號逐一算約 17 秒；一次視窗函式 4.2 秒。差別不大但這是必經路徑，
而「安全檢查放進必經路徑前要先量它多久」是這個專案的既有教訓。

⚠️ `json_group_array` 需要 SQLite 的 JSON1（3.38+ 內建）。
不可用時直接讓 migration 失敗 —— 靜默留空會讓每張帳號卡都是空的預覽區，
而那與「這個帳號沒有媒體」長得一模一樣。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None

PREVIEW_N = 4


def upgrade() -> None:
    # 純新增欄位，走原生 ADD COLUMN 不做 batch 重建 —— 理由見
    # `d4e5f6a7b8c9` 的 docstring（batch 重建在 foreign_keys=ON 下有前科）。
    op.add_column("accounts", sa.Column("preview_media", sa.Text(), nullable=True))

    conn = op.get_bind()
    conn.execute(sa.text("select json_group_array(1)"))   # JSON1 在不在，早點炸
    conn.execute(sa.text(f"""
        with ranked as (
            select p.account_id as aid, m.id as mid,
                   row_number() over (partition by p.account_id
                                      order by p.posted_at desc, m.id desc) rn
            from posts p join media m on m.post_id = p.id
        ),
        top as (
            select aid, json_group_array(mid) as js
            from ranked where rn <= {PREVIEW_N} group by aid
        )
        update accounts set preview_media = (select js from top where top.aid = accounts.id)
    """))


def downgrade() -> None:
    op.drop_column("accounts", "preview_media")
