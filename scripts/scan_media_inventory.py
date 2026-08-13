#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scan_media_inventory.py —— 舊媒體檔案盤點（唯讀）

把這支檔案複製到任一個存放媒體的目錄下，直接執行即可掃描該目錄與所有子目錄。

    python scan_media_inventory.py                      # 掃描腳本所在目錄
    python scan_media_inventory.py --root D:\\pics       # 掃描指定目錄
    python scan_media_inventory.py --label nas-old      # 給這批一個名字（多台/多處時用）
    python scan_media_inventory.py --no-hash            # 先跑一次不算 hash（最快，估時間用）
    python scan_media_inventory.py --hash full          # 全檔 sha256（慢，但可跨平台去重）
    python scan_media_inventory.py --resume             # 中斷後接續

===================== 唯讀保證 =====================
本腳本對「被掃描的目錄」只做三件事：
  1. os.scandir()  列目錄
  2. entry.stat()  讀 metadata
  3. open(path, 'rb')  唯讀開檔算 hash
全檔沒有任何 write / remove / rename / mkdir / chmod 對掃描目標的呼叫。
唯一的寫入是輸出檔，寫在 --out-dir（預設 = 本腳本所在目錄）。
====================================================

輸出兩份檔案：
  scan_<label>_<時戳>.jsonl          每個檔案一行，完整清單（可能很大）
  scan_<label>_<時戳>.summary.json   彙總統計（小，人可讀，先看這份）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

SCHEMA_VERSION = 1

# 這些副檔名標記為 is_media=True。不在清單內的檔案「照樣記錄」，只是旗標為 False。
MEDIA_EXTS = {
    # image
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".avif", ".jfif",
    ".tif", ".tiff", ".heic", ".heif", ".jxl",
    # video / audio
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".wmv", ".flv", ".ts",
    ".m3u8", ".gifv", ".m4a", ".mp3", ".wav", ".ogg", ".opus",
    # 創作原始檔（pixiv 常見）
    ".psd", ".clip", ".sai", ".sai2", ".ai", ".svg", ".zip", ".ugoira",
}

# 這些目錄不進去（進去也只會拿到 PermissionError）。
SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", "$Recycle.Bin",
    ".git", ".svn", "node_modules", "__pycache__", ".Trash-1000", "@eaDir",
}

QUICK_CHUNK = 1024 * 1024  # quick hash 取頭尾各 1 MiB

# 本腳本自己的產物。out-dir 就在掃描範圍內時（複製腳本過去執行就是這種情況），
# 不排除的話會把自己的輸出檔也記成一筆「媒體庫檔案」。
_RE_OWN_OUTPUT = re.compile(r"^scan_.*\.(jsonl|summary\.json)$")


# --------------------------------------------------------------------------
# 路徑處理
# --------------------------------------------------------------------------

def long_path(p: str) -> str:
    """Windows 上加 \\\\?\\ 前綴，繞過 260 字元 MAX_PATH 限制。

    沒有這個前綴，深層目錄或長檔名會直接丟 FileNotFoundError / OSError，
    而且錯誤訊息看起來像「檔案不存在」，非常容易誤判成掃描完整。
    """
    if os.name != "nt":
        return p
    p = os.path.abspath(p)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):  # UNC: \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def strip_long_path(p: str) -> str:
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def to_rel(path: str, root: str) -> str:
    rel = os.path.relpath(path, root)
    return rel.replace("\\", "/")


# --------------------------------------------------------------------------
# hash
# --------------------------------------------------------------------------

def hash_file(path: str, size: int, mode: str):
    """回傳 (hash_hex, algo)。任何 I/O 錯誤往上拋，由呼叫端記成 error row。

    mode:
      none  —— 不算
      quick —— sha256(size || 頭 1MiB || 尾 1MiB)。夠用來找重複檔，速度不受檔案大小影響。
      full  —— 全檔 sha256。可跟 DB 的 media.file_hash 對得上。
    """
    if mode == "none":
        return None, None

    h = hashlib.sha256()
    if mode == "full":
        with open(path, "rb") as f:
            while True:
                buf = f.read(1024 * 1024)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest(), "sha256"

    # quick
    h.update(size.to_bytes(8, "little"))
    with open(path, "rb") as f:
        h.update(f.read(QUICK_CHUNK))
        if size > QUICK_CHUNK * 2:
            f.seek(-QUICK_CHUNK, os.SEEK_END)
            h.update(f.read(QUICK_CHUNK))
    return h.hexdigest(), "quick"


# --------------------------------------------------------------------------
# 命名樣式正規化（給 summary 用，讓命名慣例一眼看得出來）
# --------------------------------------------------------------------------

_RE_DIGITS = re.compile(r"\d+")
# 純數字長串：tweet id、pixiv illust id
_RE_LONGNUM = re.compile(r"(?<![0-9A-Za-z])\d{8,}(?![0-9A-Za-z])")
# 數字+字母混合長串：misskey aid、md5/sha 檔名、CDN key
_RE_MIXEDID = re.compile(
    r"(?<![0-9A-Za-z])(?=[0-9A-Za-z]*\d)(?=[0-9A-Za-z]*[A-Za-z])[0-9A-Za-z]{8,}(?![0-9A-Za-z])"
)


def name_pattern(name: str) -> str:
    """`[2019-11-30 14-20-33] 1200781666005872640_0.jpg`
       -> `[<d>-<d>-<d> <d>-<d>-<d>] <num>_<d>.jpg`

    先代成控制字元當 placeholder、最後才展開成 `<...>`：直接代成
    `<id>` 的話，後面那道 `[A-Za-z]{2,}` 會把 `id` 兩個字母再吃掉一次，
    變成 `<<a>>`。
    """
    stem, ext = os.path.splitext(name)
    s = _RE_LONGNUM.sub("\x01", stem)
    s = _RE_MIXEDID.sub("\x02", s)
    s = _RE_DIGITS.sub("\x03", s)
    # 非 ASCII 字元收斂成 <w>，避免每個日文標題各自成一種樣式
    s = re.sub(r"[^\x00-\x7F]+", "\x04", s)
    s = re.sub(r"[A-Za-z]{2,}", "\x05", s)
    for sentinel, token in (
        ("\x01", "<num>"), ("\x02", "<id>"), ("\x03", "<d>"),
        ("\x04", "<w>"), ("\x05", "<a>"),
    ):
        s = s.replace(sentinel, token)
    return s + ext.lower()


# --------------------------------------------------------------------------
# 統計累積
# --------------------------------------------------------------------------

class Stats:
    def __init__(self, top_n_examples: int = 3):
        self.files = 0
        self.bytes = 0
        self.dirs = 0
        self.errors = 0
        self.media_files = 0
        self.media_bytes = 0
        self.by_ext = {}          # ext -> [n, bytes]
        self.by_top_dir = {}      # 第一層子目錄 -> [n, bytes]
        self.by_depth = {}        # 深度 -> n
        self.name_patterns = {}   # pattern -> [n, [examples]]
        self.dir_patterns = {}    # 目錄名 pattern -> [n, [examples]]
        self.mtime_year = {}
        self.ctime_year = {}
        self.largest = []         # [(bytes, rel)]，只留前 30
        self.err_samples = []
        self.top_n_examples = top_n_examples

    def add_file(self, rel: str, ext: str, size: int, mtime: int, ctime: int, is_media: bool):
        self.files += 1
        self.bytes += size
        if is_media:
            self.media_files += 1
            self.media_bytes += size

        e = self.by_ext.setdefault(ext, [0, 0])
        e[0] += 1
        e[1] += size

        parts = rel.split("/")
        top = parts[0] if len(parts) > 1 else "<root>"
        t = self.by_top_dir.setdefault(top, [0, 0])
        t[0] += 1
        t[1] += size

        depth = len(parts) - 1
        self.by_depth[depth] = self.by_depth.get(depth, 0) + 1

        pat = name_pattern(parts[-1])
        p = self.name_patterns.setdefault(pat, [0, []])
        p[0] += 1
        if len(p[1]) < self.top_n_examples:
            p[1].append(rel)

        if mtime:
            y = time.strftime("%Y", time.localtime(mtime))
            self.mtime_year[y] = self.mtime_year.get(y, 0) + 1
        if ctime:
            y = time.strftime("%Y", time.localtime(ctime))
            self.ctime_year[y] = self.ctime_year.get(y, 0) + 1

        if len(self.largest) < 30:
            self.largest.append((size, rel))
            self.largest.sort(reverse=True)
        elif size > self.largest[-1][0]:
            self.largest[-1] = (size, rel)
            self.largest.sort(reverse=True)

    def add_dir(self, rel: str):
        self.dirs += 1
        name = rel.split("/")[-1]
        pat = name_pattern(name)
        p = self.dir_patterns.setdefault(pat, [0, []])
        p[0] += 1
        if len(p[1]) < self.top_n_examples:
            p[1].append(rel)

    def add_error(self, rel: str, msg: str):
        self.errors += 1
        if len(self.err_samples) < 50:
            self.err_samples.append({"path": rel, "error": msg})

    def to_dict(self):
        def top(d, key, limit):
            return dict(sorted(d.items(), key=key, reverse=True)[:limit])

        return {
            "totals": {
                "files": self.files,
                "bytes": self.bytes,
                "gib": round(self.bytes / 1024 ** 3, 2),
                "dirs": self.dirs,
                "errors": self.errors,
                "media_files": self.media_files,
                "media_bytes": self.media_bytes,
                "media_gib": round(self.media_bytes / 1024 ** 3, 2),
            },
            "by_ext": {
                k: {"n": v[0], "bytes": v[1]}
                for k, v in top(self.by_ext, lambda kv: kv[1][0], 60).items()
            },
            "by_top_dir_total": len(self.by_top_dir),
            "by_top_dir": {
                k: {"n": v[0], "bytes": v[1]}
                for k, v in top(self.by_top_dir, lambda kv: kv[1][0], 1000).items()
            },
            "by_depth": dict(sorted(self.by_depth.items())),
            "name_patterns": {
                k: {"n": v[0], "examples": v[1]}
                for k, v in top(self.name_patterns, lambda kv: kv[1][0], 60).items()
            },
            "dir_patterns": {
                k: {"n": v[0], "examples": v[1]}
                for k, v in top(self.dir_patterns, lambda kv: kv[1][0], 40).items()
            },
            "mtime_year": dict(sorted(self.mtime_year.items())),
            "ctime_year": dict(sorted(self.ctime_year.items())),
            "largest": [{"bytes": b, "path": p} for b, p in self.largest],
            "error_samples": self.err_samples,
        }


# --------------------------------------------------------------------------
# 主掃描
# --------------------------------------------------------------------------

def iter_entries(root: str, stats: Stats, follow_links: bool):
    """深度優先走訪。不遞迴（避免深目錄爆 stack），不跟隨 symlink / junction。"""
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError as exc:
            stats.add_error(to_rel(cur, root), f"{type(exc).__name__}: {exc}")
            continue

        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=follow_links)
            except OSError as exc:
                stats.add_error(to_rel(entry.path, root), f"{type(exc).__name__}: {exc}")
                continue

            if is_dir:
                if entry.name in SKIP_DIRS:
                    continue
                stack.append(entry.path)
                stats.add_dir(to_rel(entry.path, root))
            else:
                yield entry


def run(args) -> int:
    raw_root = os.path.abspath(args.root)
    if not os.path.isdir(raw_root):
        print(f"[錯誤] 掃描目標不是目錄：{raw_root}", file=sys.stderr)
        return 2

    root = long_path(raw_root)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    label = args.label or re.sub(r"[^0-9A-Za-z_.-]+", "_", os.path.basename(raw_root.rstrip("\\/")) or "root")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    stats = Stats()

    if args.resume:
        existing = sorted(
            f for f in os.listdir(out_dir)
            if f.startswith(f"scan_{label}_") and f.endswith(".jsonl")
        )
        if not existing:
            print(f"[錯誤] --resume 找不到既有的 scan_{label}_*.jsonl", file=sys.stderr)
            return 2
        jsonl_path = os.path.join(out_dir, existing[-1])
        done = set()
        good_bytes = 0
        with open(jsonl_path, "rb") as f:
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # 最後一行寫到一半（中斷 / 斷電），整行不算數
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                good_bytes += len(raw)
                if "path" not in row:
                    continue
                done.add(row["path"])
                # 把先前那趟的結果餵回統計器，否則續跑產出的 summary
                # 只會涵蓋這次新掃到的檔案，前面的全部不見。
                if "bytes" in row:
                    stats.add_file(
                        row["path"], row.get("ext", ""), row["bytes"],
                        row.get("mtime", 0), row.get("ctime", 0),
                        row.get("media", False),
                    )
                elif "_err" in row:
                    stats.add_error(row["path"], row["_err"])

        # 把尾端不完整的部分砍掉再 append —— 否則新資料會直接接在半行後面，
        # 產出一行壞掉的 JSON，而且那筆記錄也永遠補不回來。
        # 註：這裡 truncate 的是本腳本自己的輸出檔，不是被掃描的媒體檔。
        dropped = os.path.getsize(jsonl_path) - good_bytes
        with open(jsonl_path, "r+b") as f:
            f.truncate(good_bytes)

        print(
            f"[resume] 續接 {os.path.basename(jsonl_path)}，已記錄 {len(done)} 筆"
            f"（捨棄尾端不完整資料 {dropped} bytes）",
            file=sys.stderr,
        )
        out_f = open(jsonl_path, "a", encoding="utf-8")
        base = jsonl_path[: -len(".jsonl")]
    else:
        done = set()
        base = os.path.join(out_dir, f"scan_{label}_{ts}")
        jsonl_path = base + ".jsonl"
        out_f = open(jsonl_path, "w", encoding="utf-8")
        out_f.write(json.dumps({
            "_meta": True,
            "schema_version": SCHEMA_VERSION,
            "label": label,
            "root": strip_long_path(root),
            "hostname": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "hash_mode": args.hash,
            "python": sys.version.split()[0],
            "platform": sys.platform,
        }, ensure_ascii=False) + "\n")

    # 排除：腳本自己、以及 out-dir 底下自己產出的 scan_*.jsonl / summary
    self_path = os.path.normcase(os.path.abspath(__file__))
    out_dir_nc = os.path.normcase(out_dir)

    def is_own_artifact(abs_path: str, name: str) -> bool:
        p = os.path.normcase(strip_long_path(abs_path))
        if p == self_path:
            return True
        return os.path.dirname(p) == out_dir_nc and bool(_RE_OWN_OUTPUT.match(name))

    t0 = time.time()
    n = 0
    interrupted = False

    try:
        for entry in iter_entries(root, stats, args.follow_links):
            rel = to_rel(entry.path, root)
            if rel in done:
                continue
            if is_own_artifact(entry.path, entry.name):
                continue

            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                stats.add_error(rel, f"stat {type(exc).__name__}: {exc}")
                out_f.write(json.dumps({"path": rel, "_err": f"stat: {exc}"}, ensure_ascii=False) + "\n")
                continue

            size = st.st_size
            ext = os.path.splitext(entry.name)[1].lower()
            is_media = ext in MEDIA_EXTS

            row = {
                "path": rel,
                "name": entry.name,
                "ext": ext,
                "bytes": size,
                # Windows 上 st_ctime 是「建立時間」—— 通常等於當初下載的時間，
                # 比 mtime（可能被複製/搬移改掉）更接近真實入手時間。
                "mtime": int(st.st_mtime),
                "ctime": int(st.st_ctime),
                "media": is_media,
            }

            if args.hash != "none" and size > 0:
                try:
                    digest, algo = hash_file(entry.path, size, args.hash)
                    row["hash"] = digest
                    row["hash_algo"] = algo
                except OSError as exc:
                    row["_err"] = f"hash: {exc}"
                    stats.add_error(rel, f"hash {type(exc).__name__}: {exc}")

            out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
            stats.add_file(rel, ext, size, row["mtime"], row["ctime"], is_media)

            n += 1
            if n % 2000 == 0:
                el = time.time() - t0
                rate = n / el if el else 0
                print(
                    f"  ... {n:,} 檔 / {stats.bytes / 1024 ** 3:,.1f} GiB"
                    f" / {el:,.0f}s / {rate:,.0f} 檔每秒",
                    file=sys.stderr,
                )
                out_f.flush()

    except KeyboardInterrupt:
        interrupted = True
        print("\n[中斷] 已寫出的部分保留。用 --resume 接續。", file=sys.stderr)
    finally:
        out_f.close()

    elapsed = time.time() - t0
    summary = {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "root": strip_long_path(root),
        "hostname": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "",
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "hash_mode": args.hash,
        "elapsed_sec": round(elapsed, 1),
        "interrupted": interrupted,
        "jsonl": os.path.basename(jsonl_path),
    }
    summary.update(stats.to_dict())

    summary_path = base + ".summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("", file=sys.stderr)
    print(f"檔案數    : {stats.files:,}（其中媒體 {stats.media_files:,}）", file=sys.stderr)
    print(f"總大小    : {stats.bytes / 1024 ** 3:,.2f} GiB", file=sys.stderr)
    print(f"目錄數    : {stats.dirs:,}", file=sys.stderr)
    print(f"錯誤      : {stats.errors:,}", file=sys.stderr)
    print(f"耗時      : {elapsed:,.0f} 秒", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"清單      : {jsonl_path}", file=sys.stderr)
    print(f"            {os.path.getsize(jsonl_path) / 1024 ** 2:,.1f} MB", file=sys.stderr)
    print(f"彙總      : {summary_path}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="舊媒體檔案盤點（唯讀，不會修改任何被掃描的檔案）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--root", default=None,
        help="要掃描的目錄。預設 = 本腳本所在的目錄",
    )
    ap.add_argument(
        "--out-dir", default=None,
        help="輸出檔要放哪。預設 = 本腳本所在的目錄",
    )
    ap.add_argument("--label", default=None, help="這批的名字，會寫進檔名（多處掃描時用來區分）")
    ap.add_argument(
        "--hash", choices=["none", "quick", "full"], default="quick",
        help="quick(預設)=size+頭尾各1MiB 的 sha256，速度幾乎不受檔案大小影響；"
             "full=全檔 sha256，可跟 DB 對得上但慢；none=不算",
    )
    ap.add_argument("--no-hash", action="store_true", help="等同 --hash none")
    ap.add_argument("--resume", action="store_true", help="接續同 label 最新一份 jsonl")
    ap.add_argument(
        "--follow-links", action="store_true",
        help="跟隨 symlink / junction（預設不跟隨，避免無窮迴圈）",
    )
    ap.add_argument(
        "--no-pause", action="store_true",
        help="跑完不要停在「按 Enter」（Windows 上直接雙擊執行時，沒有這個暫停視窗會瞬間關掉）",
    )
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    if args.root is None:
        args.root = here
    if args.out_dir is None:
        args.out_dir = here
    if args.no_hash:
        args.hash = "none"

    print(f"掃描目標  : {os.path.abspath(args.root)}", file=sys.stderr)
    print(f"輸出到    : {os.path.abspath(args.out_dir)}", file=sys.stderr)
    print(f"hash 模式 : {args.hash}", file=sys.stderr)
    print("唯讀模式  : 只做 scandir / stat / open('rb')，不會改動任何檔案", file=sys.stderr)
    print("", file=sys.stderr)

    code = run(args)

    # 雙擊執行時視窗會在這裡瞬間關掉，看不到結果也看不到錯誤訊息。
    if not args.no_pause and os.name == "nt" and sys.stdin.isatty():
        try:
            input("\n按 Enter 結束...")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
