"""`accounts` 的聚合欄維護。

## 為什麼把這四個數字存下來

`post_count` / `media_count` / `last_post_at` / `last_ingest_at` 原本是查詢時
即席算的。在 4,653 帳號 / 163 萬貼文 / 224 萬媒體的正式庫上實測：

- 最早的寫法（GROUP BY 子查詢再 outerjoin）：**4,900 ms**
- 改成相關純量子查詢 + 分頁：**436 ms**
- 但 `sort=media` 仍是 **1,906 ms** —— 排序鍵需要**全部**帳號的 media_count，
  分頁救不了，那是這個做法的硬底線

存下來之後全部變成讀一個欄位。

## ⚠️ 快取值會失準，所以必須看得見

去正規化的代價是「某條寫入路徑忘了維護」會變成靜默錯誤：數字慢慢偏掉，
畫面照樣顯示得理直氣壯。本專案的根因原則不允許這樣。

兩道防線：

1. **重算而不是加減。** 寫入路徑呼叫 `recompute()` 重新算受影響的帳號，
   不做 `+1` / `-1`。加減法要求每條路徑都精確配對，漏一次就永久偏移；
   重算只要求「有呼叫到」，而且下一次呼叫會自己修好。
2. **`check()` 比對真值**，由 `snsmediadl recount-accounts` 執行。
   **發現不一致時預設只回報、不修正** —— 自動修掉等於把 bug 藏起來，
   而數字反常本身就是「某條路徑漏了」的唯一線索。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from ..db.models import Account, Media, Post

log = logging.getLogger("snsmediadl")

# 帳號卡上放幾張預覽。改這個數字只要重算，不要 migration。
PREVIEW_N = 4

# 「這個帳號最新的 N 個媒體」。
#
# ⚠️ **刻意不加 `kind` 與 `status` 條件。** 加了之後 SQLite 會改從
# `ix_media_status` 驅動（掃 224 萬列），實測從 359 ms 變成 **3.6 分鐘**。
# 影片沒有縮圖 —— 那由前端顯示 ▶ 佔位，不是靠 SQL 濾掉。
#
# 也不濾分級：使用者要的是「一覽無遺」，濾掉會出現缺口。
_PREVIEW_SQL = text(f"""(
    select json_group_array(mid) from (
        select m.id as mid from media m join posts p on p.id = m.post_id
        where p.account_id = accounts.id
        order by p.posted_at desc, m.id desc
        limit {PREVIEW_N}
    )
)""")


def _exprs() -> dict:
    """四個聚合欄的**唯一定義**。重算與檢查共用，兩邊各寫一次一定會漂移。"""
    return {
        "post_count": (
            select(func.count()).select_from(Post)
            .where(Post.account_id == Account.id).scalar_subquery()
        ),
        "media_count": (
            select(func.count()).select_from(Media)
            .join(Post, Media.post_id == Post.id)
            .where(Post.account_id == Account.id).scalar_subquery()
        ),
        "last_post_at": (
            select(func.max(Post.posted_at))
            .where(Post.account_id == Account.id).scalar_subquery()
        ),
        "last_ingest_at": (
            select(func.max(Post.ingested_at))
            .where(Post.account_id == Account.id).scalar_subquery()
        ),
        "preview_media": _PREVIEW_SQL,
    }


def recompute(session: Session, account_ids: Iterable[int] | None = None) -> int:
    """重算聚合欄。`account_ids` 為 None 代表全部。回更新的帳號數。

    **不 commit** —— 呼叫端多半已經在一個交易裡（ingest 剛寫完貼文、
    deletion 剛刪完），在這裡 commit 會把對方的交易切成兩半。
    """
    stmt = update(Account).values(**_exprs())
    ids = None
    if account_ids is not None:
        ids = sorted({i for i in account_ids if i is not None})
        if not ids:
            return 0
        stmt = stmt.where(Account.id.in_(ids))
    result = session.execute(stmt.execution_options(synchronize_session=False))
    # 全表重算時 rowcount 才是帳號總數；指定 id 時就是那幾筆
    return result.rowcount if ids is None else len(ids)


def check(session: Session) -> list[dict]:
    """比對快取值與真值。回**不一致**的清單（空 list = 全對）。

    回的是差異明細而不是 bool：知道「有 3 個帳號不對」沒有用，
    要知道是哪幾個、差多少，才查得出是哪條寫入路徑漏了。
    """
    e = _exprs()
    rows = session.execute(
        select(
            Account.id,
            Account.screen_name,
            Account.post_count, e["post_count"],
            Account.media_count, e["media_count"],
            Account.last_post_at, e["last_post_at"],
            Account.last_ingest_at, e["last_ingest_at"],
            Account.preview_media, e["preview_media"],
        )
    ).all()

    bad = []
    for (aid, name, pc, pc_real, mc, mc_real,
         lp, lp_real, li, li_real, pv, pv_real) in rows:
        diffs = {}
        if pc != pc_real:
            diffs["post_count"] = (pc, pc_real)
        if mc != mc_real:
            diffs["media_count"] = (mc, mc_real)
        if lp != lp_real:
            diffs["last_post_at"] = (lp, lp_real)
        if li != li_real:
            diffs["last_ingest_at"] = (li, li_real)
        # 預覽是快照（貼文被刪、新採集都會變），一樣要能被 --check 抓到 ——
        # 不然它會慢慢指向不存在的媒體，而畫面只是默默顯示破圖
        if (pv or "[]") != (pv_real or "[]"):
            diffs["preview_media"] = (pv, pv_real)
        if diffs:
            bad.append({"id": aid, "screen_name": name, "diffs": diffs})
    return bad
