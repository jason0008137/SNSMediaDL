"""命令列介面。

`import` 讓 backend 不必等 extension 接線就能端對端驗收 ——
直接吃 extension 倒出的 JSON。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from .adapters import AuthRequired
from .config import load_config
from .db.enums import MediaStatus
from .db.models import Base, Media
from .db.session import make_engine
from .downloader import run_worker
from .services.ingest import ingest


def _bootstrap():
    cfg = load_config()
    engine = make_engine(cfg)
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return cfg, maker


def cmd_import(args: argparse.Namespace) -> int:
    cfg, maker = _bootstrap()

    path = Path(args.path)
    if not path.exists():
        print(f"找不到檔案：{path}", file=sys.stderr)
        return 2

    payload = json.loads(path.read_text(encoding="utf-8"))
    screen_name = args.screen_name or path.stem

    with maker() as session:
        result = ingest(session, args.platform, payload, screen_name=screen_name)

    print(f"匯入 {path.name}")
    print(f"  新增貼文 {result.posts_new}　略過 {result.posts_skipped}　新增媒體 {result.media_new}")

    if args.no_download:
        print("  (--no-download，略過下載)")
        return 0

    stats = asyncio.run(run_worker(cfg, maker))
    print(f"  下載完成 {stats.done}　略過 {stats.skipped}　失敗 {stats.failed}")
    for err in stats.errors[:10]:
        print(f"    ! {err}")
    print(f"  輸出目錄：{cfg.output_root}")
    return 1 if stats.failed else 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """對 Misskey / Mastodon / pixiv 帳號抓一次。X 不走這裡（它要 extension）。"""
    from .services.fetch import fetch_account

    cfg, maker = _bootstrap()
    host = args.host or ""
    if args.platform != "pixiv" and not host:
        print(f"錯誤：{args.platform} 需要 --host（例如 misskey.io）", file=sys.stderr)
        return 2

    # 這三種是「使用者能修的狀況」（打錯帳號、站台掛了、要 token），印一行說清楚
    # 就夠了，噴 traceback 只是難看。**不是吞掉** —— 一律 exit != 0，
    # 而且不會有「抓到 0 則」這種假裝成功的輸出。其他例外照樣讓它炸，
    # 那些是程式的 bug，堆疊有用。
    try:
        result = asyncio.run(fetch_account(
            cfg, maker,
            platform=args.platform, host=host, acct=args.acct, full=args.full,
        ))
    except ValueError as exc:
        # adapter 的 *FieldError 都是 ValueError —— 平台改版了
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    except AuthRequired as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = "（帳號名稱打錯了？）" if code == 404 else ""
        print(f"錯誤：{host or args.platform} 回 HTTP {code}{hint}", file=sys.stderr)
        print(f"       {exc.request.url}", file=sys.stderr)
        return 2
    except httpx.RequestError as exc:
        print(f"錯誤：連不上 {exc.request.url} —— {exc}", file=sys.stderr)
        return 2

    where = f"@{result.account}" + (f"@{result.instance_host}" if result.instance_host else "")
    print(f"抓取 {where}")
    if result.works_total:
        # id 清單式平台（pixiv）：把「要跑多久」講出來。
        # 不能按下去然後不知道要等多久。
        print(
            f"  作品總數 {result.works_total}　這次要抓 {result.works_to_fetch}"
            f"　預估至少 {result.estimated_seconds / 60:.1f} 分鐘"
        )
    print(f"  翻了 {result.pages} 頁，看到 {result.posts_seen} 則")
    print(f"  新增貼文 {result.posts_new}　新增媒體 {result.media_new}")
    print(f"  停止原因：{result.stopped_because}")

    if args.no_download or not result.media_new:
        return 0

    stats = asyncio.run(run_worker(cfg, maker))
    print(f"  下載完成 {stats.done}　略過 {stats.skipped}　失敗 {stats.failed}")
    for err in stats.errors[:10]:
        print(f"    ! {err}")
    return 1 if stats.failed else 0


def _run_targets(cfg, maker, jobs_in, *, full: bool, download: bool) -> int:
    """把一串目標序列跑完，逐筆印結果。

    共用 `FetchQueue`（`autostart=False`，CLI 自己 `run_all()`）——
    429 依站台隔離、一筆失敗不停整批這些規則只有一份實作。
    """
    from .services.fetch_queue import FetchQueue

    queue = FetchQueue(cfg=cfg, maker=maker, autostart=False)
    for target, user_id in jobs_in:
        if queue.enqueue(target, full=full, user_id=user_id) is None:
            print(f"  [--] {target.label} 已在這批裡，略過")

    done = asyncio.run(queue.run_all())

    failed = 0
    for job in done:
        if job.state == "done":
            r = job.result or {}
            print(
                f"  [ok] {job.label}：新增貼文 {r.get('posts_new', 0)}"
                f"　新增媒體 {r.get('media_new', 0)}　（{r.get('stopped_because', '')}）"
            )
            # 「達到頁數上限」很容易被讀成「抓完了」，批次時更容易滑過去
            if "上限" in str(r.get("stopped_because", "")):
                print("       ^ 這個帳號可能還有更舊的內容沒抓到")
        elif job.state == "skipped":
            print(f"  [--] {job.label}：{job.reason}")
        else:
            failed += 1
            print(f"  [!!] {job.label}：{job.error}", file=sys.stderr)

    if not download:
        return 1 if failed else 0

    stats = asyncio.run(run_worker(cfg, maker))
    print(f"下載完成 {stats.done}　略過 {stats.skipped}　失敗 {stats.failed}")
    for err in stats.errors[:10]:
        print(f"  ! {err}")
    return 1 if (failed or stats.failed) else 0


def cmd_fetch_urls(args: argparse.Namespace) -> int:
    """貼一堆網址批次抓。**預設是預演**，要加 --yes 才真的跑。"""
    from .urls import parse_lines

    if args.path == "-":
        text = sys.stdin.read()
    else:
        text = Path(args.path).read_text(encoding="utf-8")

    lines = parse_lines(text)
    if not lines:
        print("沒有可解析的內容（空白行與 # 開頭會被略過）", file=sys.stderr)
        return 2

    targets = []
    bad = 0
    for line in lines:
        if line.error:
            bad += 1
            print(f"  [!!] {line.raw}\n      {line.error}", file=sys.stderr)
        elif line.duplicate:
            print(f"  [--] {line.raw}（這批裡重複）")
        else:
            assert line.target is not None
            print(f"  [ok] {line.raw}  ->  {line.target.label}")
            targets.append((line.target, None))

    if not targets:
        print("沒有任何可以抓的目標", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"\n這是預演。加 --yes 才會真的抓（{len(targets)} 個帳號、{bad} 行看不懂）")
        return 0

    cfg, maker = _bootstrap()
    print()
    return _run_targets(cfg, maker, targets, full=args.full,
                        download=not args.no_download)


def cmd_refresh(args: argparse.Namespace) -> int:
    """一鍵更新：DB 裡已有的帳號各跑一次增量。"""
    from .services.fetch_queue import plan_refresh

    cfg, maker = _bootstrap()
    with maker() as session:
        plan = plan_refresh(session, cfg, include_pixiv=args.include_pixiv)

    # ⚠️ 跳過的要講出來。不講的話使用者會以為 X 的帳號也更新過了。
    reasons = {
        "cannot_fetch": "只能由 extension 採集（X）",
        "untracked": "已取消追蹤",
        "pixiv_excluded": "這次沒有包含 pixiv（加 --include-pixiv）",
        "no_credentials": "缺憑證（config.toml 的 platform_credentials）",
    }
    for key, labels in plan.skipped.items():
        print(f"跳過 {len(labels)} 個 —— {reasons.get(key, key)}：{', '.join(labels)}")

    if not plan.targets:
        print("沒有可以更新的帳號")
        return 0

    print(f"要更新 {len(plan.targets)} 個帳號")
    if not args.yes:
        print("這是預演。加 --yes 才會真的抓")
        return 0

    return _run_targets(cfg, maker, plan.targets, full=args.full,
                        download=not args.no_download)


def cmd_download(args: argparse.Namespace) -> int:
    cfg, maker = _bootstrap()
    stats = asyncio.run(run_worker(cfg, maker, limit=args.limit))
    print(f"下載完成 {stats.done}　略過 {stats.skipped}　失敗 {stats.failed}")
    for err in stats.errors[:10]:
        print(f"  ! {err}")
    return 1 if stats.failed else 0


def cmd_status(_args: argparse.Namespace) -> int:
    cfg, maker = _bootstrap()
    print(f"下載目錄　{cfg.output_root}")
    for extra in cfg.extra_media_roots:
        print(f"  另可讀取　{extra}")
    with maker() as session:
        rows = session.execute(
            select(Media.status, func.count()).group_by(Media.status)
        ).all()
    counts = {s.value: 0 for s in MediaStatus}
    counts.update(dict(rows))
    total = sum(counts.values())
    print(f"媒體總數 {total}")
    for status, n in counts.items():
        print(f"  {status:12} {n}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """備份資料庫。走 SQLite 的線上備份 API，不是複製檔案。

    WAL 模式下主檔可能落後於 WAL，`copy` 出來的備份會少掉最近的交易 ——
    而且看起來完全正常，直到你真的需要它。
    """
    from .db import recovery

    cfg, _maker = _bootstrap()
    engine = make_engine(cfg)
    target = recovery.backup(engine, cfg.db_path, tag=args.tag)
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"已備份 → {target}（{size_mb:,.0f} MB）")
    return 0


def cmd_check_db(_args: argparse.Namespace) -> int:
    """資料庫完整性檢查。**不自動修復** —— 損壞時要人來決定從哪個備份還原。"""
    from .db import recovery

    cfg, _maker = _bootstrap()
    engine = make_engine(cfg)
    result = recovery.quick_check(engine)
    print(f"完整性檢查：{result}")
    return 0 if result == "ok" else 1


def cmd_analyze(_args: argparse.Namespace) -> int:
    """跑 `ANALYZE`，讓 planner 有真實的資料分布統計。

    一次性動作。資料量變化很大之後（例如剛匯入幾百萬筆）值得再跑一次。
    """
    cfg, _maker = _bootstrap()
    engine = make_engine(cfg)
    with engine.begin() as conn:
        conn.exec_driver_sql("ANALYZE")
    print("ANALYZE 完成，統計已寫入 sqlite_stat1。")
    return 0


def cmd_identity(args: argparse.Namespace) -> int:
    """帳號身分現況：還有幾個「只有名字」的匯入帳號。

    **只有 `--list`，沒有 `--fix`。** 這不是漏做的：哨符要補上真實 id，
    得先知道那個 id 是什麼，而平台的 user id 只有在採集當下拿得到
    （X 的公開 API 已關）。離線補不了 —— 治療發生在 ingest 主路徑上，
    見 `services/identity.py`。

    這支指令的用途是**回答「還剩多少」與「補過哪些」**。
    """
    from sqlalchemy import func, select

    from .db.models import Account, IdentityHeal
    from .services.identity import PLACEHOLDER_PREFIX

    _cfg, maker = _bootstrap()
    with maker() as session:
        rows = session.execute(
            select(Account.platform, func.count())
            .where(Account.platform_user_id.like(f"{PLACEHOLDER_PREFIX}%"))
            .group_by(Account.platform)
            .order_by(func.count().desc())
        ).all()
        total = sum(n for _, n in rows)
        if rows:
            print(f"還沒有平台 id 的帳號：{total} 個")
            for platform, n in rows:
                print(f"  {platform:10} {n:>6}")
            print("\n這些帳號只有名字。造訪它們的頁面讓 extension 採集一次，")
            print("或（Fediverse）跑一次「一鍵更新」，就會自動補上。")
        else:
            print("所有帳號都有平台 id。")

        # 同名但已經有真 id 的那一列 —— 那是**下次採集就會被合併**的對象
        ghost = select(Account).where(
            Account.platform_user_id.like(f"{PLACEHOLDER_PREFIX}%")).subquery()
        pending = session.execute(
            select(ghost.c.platform, ghost.c.screen_name)
            .join(Account, (Account.platform == ghost.c.platform)
                  & (Account.instance_host == ghost.c.instance_host)
                  & (func.lower(Account.screen_name) == func.lower(ghost.c.screen_name))
                  & (Account.id != ghost.c.id))
        ).all()
        if pending:
            print(f"\n⚠️ 有 {len(pending)} 個帳號同時存在「只有名字」與「有 id」兩列：")
            for platform, name in pending[:args.limit]:
                print(f"  {platform:10} {name}")
            print("  下次採集到它們時會自動合併（貼文搬過去、哨符列刪掉）。")

        heals = session.scalars(
            select(IdentityHeal).order_by(IdentityHeal.at.desc()).limit(args.limit)
        ).all()
        if heals:
            print(f"\n最近補齊的 {len(heals)} 筆：")
            for h in heals:
                when = h.at.isoformat(sep=" ", timespec="seconds")
                what = "合併" if h.kind == "merge" else "改 id"
                extra = f"，搬了 {h.moved_posts} 則貼文" if h.kind == "merge" else ""
                print(f"  {when}  {h.platform:8} @{h.screen_name} {what}"
                      f"（{h.placeholder_id} → {h.real_id}）{extra}")
    return 0


def cmd_recount_accounts(args: argparse.Namespace) -> int:
    """檢查（或修正）`accounts` 的聚合欄。

    **`--check` 是預設，`--fix` 才會寫入。**

    為什麼不做成「發現不一致就自動修好」：那等於把 bug 藏起來。
    這四個欄位是快取值，不一致代表**某條寫入路徑漏了維護** ——
    數字本身就是那條路徑存在的唯一線索。默默修掉，下次還是會偏，
    而且永遠查不出是誰造成的。
    """
    from .services import counters

    _cfg, maker = _bootstrap()
    with maker() as session:
        bad = counters.check(session)

        if not bad:
            print("聚合欄與真值一致。")
            return 0

        print(f"⚠️ {len(bad)} 個帳號的聚合欄與真值不一致：\n")
        for row in bad[:args.limit]:
            name = row["screen_name"] or f"id={row['id']}"
            print(f"  account#{row['id']}　{name}")
            for field, (cached, real) in row["diffs"].items():
                print(f"    {field:16} 存的 {cached!r} → 真值 {real!r}")
        if len(bad) > args.limit:
            print(f"  …另有 {len(bad) - args.limit} 個（用 --limit 看更多）")

        if not args.fix:
            print("\n這是檢查，什麼都沒改。要重算請加 --fix。")
            print("⚠️ 重算之前先想一下：是哪條寫入路徑漏了 counters.recompute()？")
            # 非 0 退出 —— 這樣它才能被排進自動檢查而不是靠人記得看輸出
            return 1

        n = counters.recompute(session, [r["id"] for r in bad])
        session.commit()
        print(f"\n已重算 {n} 個帳號。")

        left = counters.check(session)
        if left:
            # 重算完還是不對，那不是「漏了維護」而是定義本身有問題
            print(f"❌ 重算後仍有 {len(left)} 個不一致 —— "
                  "counters._exprs 與 migration 的 backfill 可能算法不同",
                  file=sys.stderr)
            return 2
    return 0


def cmd_delete_account(args: argparse.Namespace) -> int:
    """刪掉一個帳號的全部記錄。**dry-run 是預設，不是選項。**"""
    from .services import deletion

    _cfg, maker = _bootstrap()
    with maker() as session:
        try:
            summary = deletion.preview_account_deletion(session, args.account_id)
        except LookupError as exc:
            print(f"錯誤：{exc}", file=sys.stderr)
            return 2

        print(f"account#{summary.account_id}　{summary.screen_name}（{summary.platform}）")
        print(f"  會刪除：{summary.posts} 則貼文、{summary.media} 筆媒體記錄")
        for w in summary.warnings:
            print(f"  ⚠️ {w}")
        print("  本機媒體檔案不會被刪除。")

        if not args.yes:
            print("\n這是預演，什麼都沒動。要真的刪請加 --yes。")
            return 0

        result = deletion.delete_account(session, args.account_id)

    print(f"\n已刪除 {result.posts} 則貼文、{result.media} 筆媒體記錄。")
    print(f"{result.downloaded_files_kept} 個檔案留在磁碟上。")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    cfg = load_config()
    host = args.host or cfg.host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"警告：綁定 {host} 會把下載歷史對外開放，而本專案刻意不做認證。",
            file=sys.stderr,
        )
    if cfg.auto_download:
        print(f"背景下載已啟用（每 {cfg.poll_interval_seconds:g} 秒檢查佇列）")
    uvicorn.run(
        create_app(cfg, enable_worker=True),
        host=host,
        port=args.port or cfg.port,
        log_level="info",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="snsmediadl", description="SNS 媒體下載器")
    sub = p.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="匯入 extension 倒出的 JSON 並下載")
    imp.add_argument("path")
    imp.add_argument("--platform", default="x")
    imp.add_argument("--screen-name", default=None)
    imp.add_argument("--no-download", action="store_true", help="只入庫不下載")
    imp.set_defaults(func=cmd_import)

    fe = sub.add_parser("fetch", help="從 Misskey / Mastodon / pixiv 抓一個帳號")
    fe.add_argument("acct", help="帳號名稱，可帶 @。pixiv 要數字 user id")
    fe.add_argument(
        "--platform", required=True, choices=["misskey", "mastodon", "pixiv"]
    )
    fe.add_argument(
        "--host", default=None,
        help="instance，例如 misskey.io（pixiv 是單一站台，不需要）",
    )
    fe.add_argument("--full", action="store_true",
                    help="不因碰到已抓過的貼文就停（用於補抓中間漏掉的）")
    fe.add_argument("--no-download", action="store_true", help="只入庫不下載")
    fe.set_defaults(func=cmd_fetch)

    fu = sub.add_parser(
        "fetch-urls",
        help="貼一堆網址批次抓（misskey / baraag / pixiv）。預設是預演",
    )
    fu.add_argument("path", help="每行一個網址的檔案，或 - 讀 stdin")
    fu.add_argument("--yes", action="store_true", help="真的抓。不加就只印解析結果")
    fu.add_argument("--full", action="store_true",
                    help="不因碰到已抓過的貼文就停（用於補抓中間漏掉的）")
    fu.add_argument("--no-download", action="store_true", help="只入庫不下載")
    fu.set_defaults(func=cmd_fetch_urls)

    rf = sub.add_parser(
        "refresh", help="一鍵更新：DB 裡已有的帳號各跑一次增量。預設是預演"
    )
    rf.add_argument("--yes", action="store_true", help="真的抓")
    rf.add_argument("--include-pixiv", action="store_true",
                    help="一併更新 pixiv（節流很慢，預設不含）")
    rf.add_argument("--full", action="store_true", help="不提早停")
    rf.add_argument("--no-download", action="store_true", help="只入庫不下載")
    rf.set_defaults(func=cmd_refresh)

    dl = sub.add_parser("download", help="把佇列裡待下載的媒體抓完")
    dl.add_argument("--limit", type=int, default=None)
    dl.set_defaults(func=cmd_download)

    st = sub.add_parser("status", help="佇列統計")
    st.set_defaults(func=cmd_status)

    bk = sub.add_parser("backup", help="備份資料庫（線上備份 API，不是複製檔案）")
    bk.add_argument("--tag", default="manual", help="備份檔名裡的用途標籤")
    bk.set_defaults(func=cmd_backup)

    ck = sub.add_parser("check-db", help="資料庫完整性檢查（不自動修復）")
    ck.set_defaults(func=cmd_check_db)

    an = sub.add_parser("analyze", help="跑 ANALYZE，更新 planner 的統計")
    an.set_defaults(func=cmd_analyze)

    rc = sub.add_parser(
        "recount-accounts",
        help="檢查 accounts 的聚合欄有沒有跟真值對上。預設只檢查不修正",
    )
    rc.add_argument(
        "--fix", action="store_true",
        help="真的重算寫回。不加這個就只是回報差異（不一致時 exit code 1）",
    )
    rc.add_argument("--limit", type=int, default=20, help="最多列出幾筆差異")
    rc.set_defaults(func=cmd_recount_accounts)

    idt = sub.add_parser(
        "identity",
        help="還有幾個「只有名字」的匯入帳號，以及補齊過哪些",
    )
    idt.add_argument("--list", action="store_true", help="（預設行為，留著讓意圖明確）")
    idt.add_argument("--limit", type=int, default=20, help="最多列出幾筆")
    idt.set_defaults(func=cmd_identity)

    da = sub.add_parser(
        "delete-account",
        help="刪掉一個帳號的全部記錄（不刪本機檔案）。預設是預演",
    )
    da.add_argument("account_id", type=int)
    da.add_argument(
        "--yes", action="store_true",
        help="真的執行刪除。不加這個就只是印出會刪什麼",
    )
    da.set_defaults(func=cmd_delete_account)

    sv = sub.add_parser("serve", help="啟動 API（預設只綁 localhost）")
    sv.add_argument("--host", default=None)
    sv.add_argument("--port", type=int, default=None)
    sv.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
