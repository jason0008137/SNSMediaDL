"""平台名 `baraag` → `mastodon`

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-15

## 為什麼

匯入器把 EpicDL 的目錄名 `__BR` 直接當成平台名寫進 DB（`baraag`），
但 adapter 註冊表裡這個平台叫 `mastodon`。後果不是報錯，是**整批靜默消失**：

    can_fetch('baraag') → get_adapter 丟 ValueError → False
                        → plan_refresh 歸進 cannot_fetch
                        → 介面顯示「只能由 extension 採集（X）」

使用者按「一鍵更新」，12 個 baraag 帳號完全沒被納入，而畫面上給的理由
還是錯的（那句話是為 X 寫的）。正式庫實測：accounts 12 筆、posts 1,631 筆。

平台名是**全鏈路的唯一鍵成分**（`accounts` 與 `posts` 的唯一鍵都含它），
所以這不是「加個別名就好」的問題 —— 兩個名字並存的話，同一個人從網址抓
（會解析成 `mastodon`）與匯入來的（`baraag`）會變成兩列，媒體從此分兩半。

## 撞鍵檢查

改名等於把 `(baraag, host, id)` 搬到 `(mastodon, host, id)`。如果目標已經
存在，UPDATE 會違反唯一鍵。正式庫實測 `mastodon` 是 0 筆，但別人的庫不一定 ——
所以先檢查，撞到就**中止並列出來**，不做「跳過那幾筆」那種靜默處理。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

OLD = "baraag"
NEW = "mastodon"


def _guard(conn, table: str, key_cols: tuple[str, ...], src: str, dst: str) -> None:
    """改名會不會撞唯一鍵。撞到就中止 —— 靜默跳過等於留下一半沒搬的資料。"""
    cols = ", ".join(key_cols)
    joins = " and ".join(f"a.{c} = b.{c}" for c in key_cols)
    clash = conn.execute(
        sa.text(
            f"select a.{key_cols[-1]} from {table} a join {table} b on {joins} "
            f"and b.platform = :dst where a.platform = :src limit 20"
        ),
        {"src": src, "dst": dst},
    ).fetchall()
    if clash:
        raise RuntimeError(
            f"{table} 有 {len(clash)} 筆以上改名後會撞唯一鍵（{cols}）："
            f"{[r[0] for r in clash]}。"
            "這代表同一個帳號已經同時以 baraag 與 mastodon 存在，"
            "要先決定保留哪一列（本 migration 不替你猜）。"
        )


def _rename(src: str, dst: str) -> None:
    conn = op.get_bind()
    _guard(conn, "accounts", ("instance_host", "platform_user_id"), src, dst)
    _guard(conn, "posts", ("instance_host", "platform_post_id"), src, dst)
    for table in ("accounts", "posts"):
        conn.execute(
            sa.text(f"update {table} set platform = :dst where platform = :src"),
            {"src": src, "dst": dst},
        )


def upgrade() -> None:
    _rename(OLD, NEW)


def downgrade() -> None:
    # ⚠️ 反向會把**所有** mastodon 資料標成 baraag，包含不是從 EpicDL 匯入的那些。
    # 這個 migration 之後才用網址抓進來的 mastodon 帳號會一起被改到 —— 因為
    # 資料本身沒有留下「我原本叫 baraag」的痕跡，也不該為了可逆而加一個欄位。
    # 只在「剛升級完就要退回去」的情況下用它。
    _rename(NEW, OLD)
