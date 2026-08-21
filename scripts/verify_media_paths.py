#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""逐一確認 `media.local_path` 指到的檔案還在不在。

===================== 唯讀 =====================
**一個位元組都不寫回 DB。** 輸出只有一份 JSON 報告。

「哪些檔案不見了要怎麼處理」是看完報告之後的另一件事 —— 自動把記錄標成
遺失、或自動刪掉，都是在使用者還沒看過名單之前替他做決定。
================================================

## 為什麼需要這支

`media.local_path` 是**匯入當下記下的字串**，沒有任何機制驗證過檔案還在不在。
搬過檔、刪過檔、換過磁碟代號，DB 都不會知道。

⚠️ 這支腳本會 stat 每一個檔案。正式庫是 232 萬筆散在三顆碟上，
機械碟大約是**以小時計**的。跑之前先想清楚要不要用 `--drive` 縮範圍。

## 長路徑

檢查一律走 `snsmediadl.fspath.for_io`（`\\?\` 前綴）。少了它，超過 260 字元
的路徑會被 Windows 說成「不存在」—— 那正是 2026-08-21 的假 404 的來源，
而這支腳本要是也犯同一個錯，報告就會把 606 個好好的檔案列成遺失。

用法：

    python scripts/verify_media_paths.py --db K:\\...\\snsmediadl.db
    python scripts/verify_media_paths.py --db ... --drive K      # 只驗 K:
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from snsmediadl.fspath import for_io  # noqa: E402

# 進度回報的間隔。太小會被 I/O 蓋掉，太大則是「跑了半小時沒有任何輸出」——
# 而那會讓人以為當掉了，然後按下 Ctrl-C。
PROGRESS_EVERY = 10_000

# 報告裡放幾筆樣本。全部列出來的話，一個搬過目錄的媒體庫會生出上百 MB 的 JSON。
SAMPLE_LIMIT = 50

# Windows 的 MAX_PATH。長度 >= 這個數字的路徑，沒有 `\\?\` 前綴就開不了。
MAX_PATH = 260


def drive_of(p: str) -> str:
    """`K:\\a\\b` -> `K:`；UNC -> `\\\\server\\share`；都不是就回 `?`。"""
    if p.startswith("\\\\"):
        parts = p.split("\\")
        return "\\\\" + "\\".join(parts[2:4]) if len(parts) >= 4 else "\\\\?"
    if len(p) >= 2 and p[1] == ":":
        return p[:2].upper()
    return "?"


def verify(db: Path, drive: str | None) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    total_rows = con.execute(
        "SELECT count(*) FROM media WHERE local_path IS NOT NULL"
    ).fetchone()[0]

    ok: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    long_ok: Counter[str] = Counter()
    long_missing: Counter[str] = Counter()
    samples: list[dict] = []
    checked = skipped = 0

    want = drive.rstrip(":").upper() + ":" if drive else None
    print(f"共 {total_rows:,} 筆有路徑的媒體記錄"
          + (f"，只驗 {want}" if want else "") + "。開始。", flush=True)

    cur = con.execute(
        "SELECT id, local_path FROM media WHERE local_path IS NOT NULL ORDER BY id"
    )
    for mid, path in cur:
        dev = drive_of(path)
        if want and dev != want:
            skipped += 1
            continue
        checked += 1
        is_long = len(path) >= MAX_PATH
        # ⚠️ 一律走 for_io。這一行就是這支腳本會不會自己製造假遺失的分水嶺。
        if for_io(path).exists():
            ok[dev] += 1
            if is_long:
                long_ok[dev] += 1
        else:
            missing[dev] += 1
            if is_long:
                long_missing[dev] += 1
            if len(samples) < SAMPLE_LIMIT:
                samples.append({"media_id": mid, "local_path": path,
                                "drive": dev, "length": len(path)})
        if checked % PROGRESS_EVERY == 0:
            print(f"  {checked:,} / {total_rows:,}   缺 {sum(missing.values()):,}",
                  flush=True)

    con.close()
    return {
        "db": str(db),
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "drive_filter": want,
        "rows_with_path": total_rows,
        "checked": checked,
        "skipped_by_filter": skipped,
        "ok": sum(ok.values()),
        "missing": sum(missing.values()),
        "by_drive": {
            dev: {"ok": ok[dev], "missing": missing[dev],
                  "long_ok": long_ok[dev], "long_missing": long_missing[dev]}
            for dev in sorted(set(ok) | set(missing))
        },
        "long_paths": {
            "note": f"length >= {MAX_PATH}；沒有 \\\\?\\ 前綴就開不了",
            "ok": sum(long_ok.values()),
            "missing": sum(long_missing.values()),
        },
        "missing_samples": samples,
    }


def _utf8_when_piped() -> None:
    """輸出被導向檔案／管線時改用 UTF-8。

    Windows 的實體主控台走 WriteConsoleW，什麼字元都印得出來；但一旦被導向，
    Python 會退回系統 ANSI codepage（這台是 cp950），而報告裡任何一個
    cp950 沒有的字元都會讓整支腳本**在印報告的途中**炸掉 —— 明明工作已經做完。
    """
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _utf8_when_piped()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, type=Path, help="要驗的 snsmediadl.db")
    ap.add_argument("--drive", help="只驗這一顆碟（例如 K 或 K:）。"
                                    "某顆碟沒插時不必讓整份報告作廢。")
    ap.add_argument("--out", type=Path, default=Path.cwd(), help="報告寫到哪個目錄")
    args = ap.parse_args()

    if not args.db.is_file():
        print(f"找不到 DB：{args.db}", file=sys.stderr)
        return 2

    report = verify(args.db, args.drive)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = args.out / f"verify_paths_{stamp}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"檢查 {report['checked']:,} 筆："
          f"在 {report['ok']:,}｜不見 {report['missing']:,}")
    for dev, n in report["by_drive"].items():
        print(f"  {dev}  在 {n['ok']:,}｜不見 {n['missing']:,}"
              f"（其中長路徑：在 {n['long_ok']:,}｜不見 {n['long_missing']:,}）")
    if report["missing"]:
        print("\n前幾筆不見的：")
        for s in report["missing_samples"][:5]:
            print(f"  #{s['media_id']}  {s['local_path']}")
    print(f"\n報告：{out}")
    print("**沒有寫回 DB。** 要怎麼處理這些記錄是下一個決定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
