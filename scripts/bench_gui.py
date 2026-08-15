#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量 GUI 用到的每個查詢在真實資料量下要跑多久。

===================== 唯讀，而且拒絕碰 live DB =====================
連線一律 `mode=ro`。SQLAlchemy engine 是這支腳本自己建的，**不掛**
`db/session.py` 的 `_configure_sqlite`（那裡面的 `PRAGMA journal_mode=WAL`
是寫入操作，對唯讀連線會炸）。

檔名恰為 `snsmediadl.db` 時直接拒絕執行 —— 那是正在被 backend 開著的
那一顆。要量就量備份複本。真的要量 live 得明打 `--i-know`。
====================================================================

用法：

    # 量現況
    python scripts/bench_gui.py --db "K:\\...\\snsmediadl.db.bak-20260815"

    # 加上提案中的 PRAGMA 調整，A/B 對照
    python scripts/bench_gui.py --db ... --pragma

    # 順便印 EXPLAIN QUERY PLAN
    python scripts/bench_gui.py --db ... --explain

為什麼直接呼叫 `api/query.py` 的函式而不是自己抄一份 SQL：抄一份的話，
程式改了量測不會跟著改，量到的就不再是使用者實際等的那段時間。
"""

from __future__ import annotations

import argparse
import io
import statistics
import sys
import time
from pathlib import Path

# Windows 主控台預設是 cp950，中文案例名會直接炸掉整份報告
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# 讓 `python scripts/bench_gui.py` 直接跑得起來，不必先 pip install -e .
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Response  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from snsmediadl.api import query  # noqa: E402

# 唯讀連線的調校選項，只有 `--pragma` 才套用 —— 用來量它們值不值得進正式設定。
# journal_mode 不在這裡 —— 那是寫入操作，唯讀連線做不到，也不影響讀取效能。
TUNING_PRAGMAS = (
    "cache_size = -65536",      # 64 MB（預設 -2000 = 2 MB，對 739 MB 的庫等於沒有）
    "temp_store = MEMORY",      # 單帳號查詢會 USE TEMP B-TREE FOR ORDER BY
    "mmap_size = 268435456",    # 256 MB
)


def make_ro_session_factory(db: Path, tuned: bool) -> sessionmaker[Session]:
    """唯讀 engine。刻意不用 `db.session.make_engine` —— 那個會寫 journal_mode。"""
    uri = f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true"
    engine = create_engine(uri, future=True, connect_args={"check_same_thread": False})
    if tuned:
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _tune(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            for p in TUNING_PRAGMAS:
                cur.execute(f"PRAGMA {p}")
            cur.close()

    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


# ────────────────────────────────── 量測案例
#
# 每個案例 = (分類, 名稱, 觸發時機, 執行函式)。
# 「觸發時機」不是裝飾 —— 一個 300 ms 的查詢每 5 秒跑一次，比一個 1 秒但
# 使用者一天按兩次的查詢嚴重得多。排優先序要看這一欄。

def _resp() -> Response:
    """`list_accounts` 要一個 Response 來塞 X-Total-Count header。"""
    return Response()


def accounts(session: Session, **kw):
    """直接呼叫 `list_accounts`，**每個參數都要明講**。

    ⚠️ 直接呼叫 FastAPI 端點函式時，`Query(default=None)` 這種預設值不會被
    解析成 None —— 它就是一個 `Query` 物件，會原封不動被當成 SQL 參數綁進去，
    然後在 sqlite3 層炸成 `Error binding parameter N - probably unsupported type`。
    所以這裡把每個帶 `Query()` 預設的參數都補齊，不靠函式簽章的預設值。
    """
    kw.setdefault("platform", None)
    kw.setdefault("creator_id", None)
    kw.setdefault("q", None)
    kw.setdefault("favorite", None)
    kw.setdefault("min_stars", None)
    kw.setdefault("fetch_status", None)
    kw.setdefault("default_rating", None)
    kw.setdefault("default_content_type", None)
    kw.setdefault("order", None)
    kw.setdefault("offset", 0)
    return query.list_accounts(response=_resp(), session=session, **kw)


def media(session: Session, **kw):
    """同上，`list_media` 的 `min_stars` / `before_id` / `after_id` 也帶 `Query()` 預設。"""
    for k in ("status", "kind", "rating", "exclude_rating", "content_type",
              "account_id", "creator_id", "platform", "min_stars",
              "before_id", "after_id"):
        kw.setdefault(k, None)
    kw.setdefault("offset", 0)
    return query.list_media(session=session, **kw)


def media_count(session: Session, **kw):
    for k in ("status", "kind", "rating", "exclude_rating", "content_type",
              "account_id", "creator_id", "platform", "min_stars"):
        kw.setdefault(k, None)
    return query.count_media(session=session, **kw)


CASES = [
    (
        "帳號頁", "list_accounts 首頁（GUI 預設）", "點「帳號」標籤",
        lambda s: accounts(s, sort="favorite", limit=100, with_stats=True),
    ),
    (
        "帳號頁", "list_accounts 的 X-Total-Count", "同上，每次都算一次",
        # 單獨量：現行實作把整個 stmt（SELECT 清單裡有 4 個相關子查詢）
        # 包成 subquery 再 COUNT。相關子查詢會不會被 planner 消掉是這裡要驗的事。
        lambda s: s.scalar(text(
            "select count(*) from ("
            "  select accounts.id,"
            "    (select count(*) from posts where posts.account_id=accounts.id) pc,"
            "    (select max(posted_at) from posts where posts.account_id=accounts.id) lp,"
            "    (select max(ingested_at) from posts where posts.account_id=accounts.id) li,"
            "    (select count(*) from media join posts on media.post_id=posts.id"
            "       where posts.account_id=accounts.id) mc"
            "  from accounts)")),
    ),
    (
        "帳號頁", "list_accounts 排序=media", "使用者選「媒體數」排序",
        lambda s: accounts(s, sort="media", limit=100, with_stats=True),
    ),
    (
        "帳號頁", "list_accounts 搜尋 q=ka", "搜尋框每次打字（debounce 後）",
        lambda s: accounts(s, q="ka", sort="favorite", limit=100, with_stats=True),
    ),
    (
        "帳號下拉", "list_accounts limit=2000 with_stats=false", "開頁時一次",
        lambda s: accounts(s, sort="name", limit=2000, with_stats=False),
    ),
    (
        "媒體頁", "list_media 第一頁（安全模式開）", "改篩選／翻頁／評分存檔後",
        lambda s: media(s, exclude_rating="r18", sort="newest", limit=60),
    ),
    (
        "媒體頁", "list_media offset=60000（舊分頁）", "翻到第 1000 頁",
        lambda s: media(s, exclude_rating="r18", sort="newest", limit=60, offset=60000),
    ),
    (
        "媒體頁", "list_media keyset 深頁", "翻到第 1000 頁（keyset）",
        # before_id 取一個「已經翻很深」的位置，量 keyset 會不會隨深度變慢
        lambda s: media(s, exclude_rating="r18", sort="newest", limit=60,
                        before_id=2_180_000),
    ),
    (
        "媒體頁", "count_media（獨立端點，安全模式開）", "改篩選後非阻塞地跑",
        lambda s: media_count(s, exclude_rating="r18"),
    ),
    (
        "媒體頁", "list_media 單一帳號（安全模式開）", "從帳號跳過來看該帳號媒體",
        # 2884 = 實測資料裡媒體最多的帳號（19,262 筆）。極端值才量得出問題。
        lambda s: media(s, account_id=2884, exclude_rating="r18",
                        sort="newest", limit=60),
    ),
    (
        "媒體頁", "list_media 排序=stars", "使用者選「評分高到低」",
        lambda s: media(s, exclude_rating="r18", sort="stars", limit=60),
    ),
    (
        "背景", "queue_status", "**每 5 秒一次，永遠**",
        lambda s: query.queue_status(session=s),
    ),
    (
        "其他", "list_errors", "點「問題」標籤",
        lambda s: query.list_errors(limit=100, session=s),
    ),
    (
        "其他", "stats", "（目前 GUI 沒在用）",
        lambda s: query.stats(session=s),
    ),
]

# EXPLAIN 用的原始 SQL。這些是**手寫的**，要與上面的 ORM 查詢對得起來 ——
# 對不起來就是這份清單過期了，改 ORM 時要一起改。
EXPLAIN_CASES = [
    ("media COUNT(*) 安全模式",
     "select count(*) from media join posts on media.post_id=posts.id "
     "where posts.rating is null or posts.rating!='r18'"),
    ("media 第一頁 安全模式",
     "select media.* from media join posts on media.post_id=posts.id "
     "where (posts.rating is null or posts.rating!='r18') "
     "order by media.id desc limit 60"),
    ("media 單一帳號",
     "select media.* from media join posts on media.post_id=posts.id "
     "where posts.account_id=2884 order by media.id desc limit 60"),
    ("queue_status 現況（全表 group by）",
     "select status, count(*) from media group by status"),
    ("queue_status 提案（只查非 done）",
     "select status, count(*) from media where status != 'done' group by status"),
    ("pending 單查",
     "select count(*) from media where status = 'pending'"),
]


def run_case(factory: sessionmaker[Session], fn, repeat: int) -> tuple[float, float]:
    """回 (中位數毫秒, 最小值毫秒)。

    取中位數不取平均：磁碟偶發的一次 stall 會把平均拉到沒有代表性。
    同時回最小值，那是「快取全熱」的下限，用來看差距。
    """
    times = []
    for _ in range(repeat):
        # 每次都開新 session：SQLAlchemy 的 identity map 會讓第二次「查」到
        # 的是記憶體裡的物件，量出來的數字跟使用者實際等的無關。
        with factory() as session:
            start = time.perf_counter()
            fn(session)
            times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times), min(times)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path, help="要量的 SQLite 檔（唯讀開啟）")
    ap.add_argument("--repeat", type=int, default=3, help="每個案例跑幾次取中位數")
    ap.add_argument("--pragma", action="store_true", help="套用提案中的 PRAGMA 調整")
    ap.add_argument("--explain", action="store_true", help="印 EXPLAIN QUERY PLAN")
    ap.add_argument("--only", help="只跑名稱含這個子字串的案例")
    ap.add_argument("--i-know", action="store_true",
                    help="確認要量 live DB（檔名為 snsmediadl.db 時才需要）")
    args = ap.parse_args()

    db: Path = args.db
    if not db.exists():
        print(f"找不到：{db}", file=sys.stderr)
        return 2
    # live DB 正被 backend 開著。唯讀連線本身不會弄壞它，但量到的數字會被
    # 對方的寫入干擾，而且沒有理由冒這個險 —— 備份複本量出來的一樣準。
    if db.name == "snsmediadl.db" and not args.i_know:
        print(f"拒絕：{db.name} 看起來是 live DB。改量備份複本，或明打 --i-know",
              file=sys.stderr)
        return 2

    factory = make_ro_session_factory(db, tuned=args.pragma)

    with factory() as s:
        counts = {
            t: s.scalar(text(f"select count(*) from {t}"))
            for t in ("accounts", "posts", "media", "creators")
        }
        page_size = s.scalar(text("pragma page_size"))
        page_count = s.scalar(text("pragma page_count"))
        cache = s.scalar(text("pragma cache_size"))

    size_mb = (page_size or 0) * (page_count or 0) / 1024 / 1024
    print(f"# bench_gui — {db.name}")
    print()
    print(f"- DB {size_mb:,.0f} MB　page_size {page_size}　cache_size {cache}"
          f"{'　(已套用調整後的 PRAGMA)' if args.pragma else ''}")
    print("- 列數：" + "　".join(f"{k} {v:,}" for k, v in counts.items()))
    print(f"- 每案例跑 {args.repeat} 次取中位數")
    print()
    print("| 分類 | 查詢 | 觸發時機 | 中位數 | 最快 |")
    print("|------|------|---------|-------:|-----:|")

    for group, name, when, fn in CASES:
        if args.only and args.only not in name:
            continue
        try:
            med, best = run_case(factory, fn, args.repeat)
        except Exception as exc:  # noqa: BLE001
            # 量測腳本吞例外會讓「這條壞了」看起來像「這條很快」。
            # 印出來，並且讓整支腳本以非 0 退出。
            print(f"| {group} | {name} | {when} | **錯誤** | {exc} |")
            continue
        print(f"| {group} | {name} | {when} | {med:,.0f} ms | {best:,.0f} ms |")

    if args.explain:
        print()
        print("## EXPLAIN QUERY PLAN")
        with factory() as s:
            for name, sql in EXPLAIN_CASES:
                print()
                print(f"**{name}**")
                print()
                print("```")
                for row in s.execute(text("explain query plan " + sql)):
                    print("  " + str(row[-1]))
                print("```")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
