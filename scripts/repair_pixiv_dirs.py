#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pixiv 目錄補上 uid 之後，把資料庫修回來。

pixiv 的輸出目錄原本只用暱稱命名，而暱稱會改、也會撞名。改成帶 uid 之後，
資料庫裡既有的 `media.local_path` 全部指向舊路徑 —— 這支腳本把它們改過來。

===================== 不碰媒體檔 =====================
輸入是 `scan_*.jsonl` 與目標 SQLite，輸出只有那個 SQLite。
全程沒有 open() / stat() / scandir() 打在任何媒體路徑上 ——
新舊路徑的對應完全由兩份掃描檔推導出來。與 `import_media_inventory.py` 同一條紀律。
======================================================

## 在修什麼

匯入既有媒體庫時，目錄名沒有 uid 的 pixiv 帳號被寫成
`platform_user_id = 'sn:<名字>'` 哨符。pixiv adapter 只吃數字 user id
（沒有「名字 → id」這條路），所以一鍵更新走到那些帳號必然丟

    PixivFieldError: pixiv 要數字 user id，收到 '望月けい'

使用者 2026-08-16 回頭把目錄名補成 `<名字>_<uid>` 並重掃。於是 DB 有**兩處**
對不上磁碟，兩處都要修：

1. **身分** —— 哨符換成真 uid（真列已存在就合併）
2. **路徑** —— 目錄改名了，`media.local_path` 全部指向不存在的舊路徑。
   只修身分不修路徑的話，一鍵更新會過，但檢視器一張圖都開不出來

## 為什麼不重跑匯入器

`media` 的唯一鍵是 `(post_id, ordinal)`。重跑只會 `INSERT OR IGNORE` 掉，
舊的 `local_path` 原封不動 —— 修不到問題，還會多出一批沒有貼文的空帳號列。

用法：

    # 預設 dry-run —— 只報告，什麼都不寫
    python scripts/repair_pixiv_dirs.py --db K:\\...\\snsmediadl.db

    # 真的寫入（先備份 DB）
    python scripts/repair_pixiv_dirs.py --db ... --commit
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 與 `services/identity.py` 的 `PLACEHOLDER_PREFIX` 同一個約定。
# 這支腳本走原生 sqlite3（與匯入器同理由：一次性工具不綁 ORM），
# 所以只能重述而不能 import —— 兩邊要一起改。
PLACEHOLDER_PREFIX = "sn:"

# 目錄名 `<prefix>_<名字>[_<uid>]`，與匯入器的 `RE_PDIR` / `RE_UID_TAIL` 同義。
RE_PDIR = re.compile(r"^(?P<prefix>u|f|AI|3D|m|ma|j)_(?P<rest>.+)$")
RE_UID_TAIL = re.compile(r"^(?P<name>.+)_(?P<uid>\d{2,9})$")

# 合併時要從哨符列繼承過來的偏好欄位（真列沒設過的才繼承）。
# 與 `identity.heal_placeholder_account` 同一份清單。
INHERIT_FIELDS = ("is_favorite", "stars", "default_rating",
                  "default_content_type", "creator_id", "role")


# ────────────────────────────────── 掃描批次

@dataclass
class ScanBatch:
    """一批「改名前後各掃過一次」的目錄。"""
    label: str
    root: str          # 磁碟上的絕對路徑，`local_path` 的前綴
    old_glob: str
    new_glob: str


SCAN_BATCHES = [
    ScanBatch("PIXIV1", r"F:\Data\Illasuto\_Pixiv_1",
              "scan_PIXIV1_2*.jsonl", "scan_PIXIV1_FIX_*.jsonl"),
    ScanBatch("PIXIV2", r"F:\Data\Illasuto\_Pixiv_2",
              "scan_PIXIV2_2*.jsonl", "scan_PIXIV2_FIX_*.jsonl"),
]


# ────────────────────────────────── 使用者裁示（I: 那批）
#
# `I:\Exception\地獄\Hentai` 沒有重掃，而且它壞的原因跟 _Pixiv_1 不同 ——
# 是匯入器的 uid 解析漏洞（uid 不在字尾、或 uid 在第二層目錄）。
# 這裡的每一筆都是使用者 2026-08-16 逐帳號裁示的結果，不是程式推導的。
#
# 規則寫成資料不是 if-else 串：使用者再看一眼就可能改，改資料表比改控制流安全。

HENTAI_ROOT = r"I:\Exception\地獄\Hentai"


@dataclass
class Ruling:
    """對一個哨符帳號的處置。三選一。"""
    placeholder: str                     # 哨符 id（含 `sn:` 前綴）
    action: str                          # heal | split | exclude
    why: str
    uid: str | None = None               # action=heal
    # action=split：第二層（含更深）目錄 -> (uid, screen_name)。
    # 鍵是相對於帳號目錄的路徑，用反斜線。
    parts: dict[str, tuple[str, str]] = field(default_factory=dict)
    # action=split 時，沒有對應到任何 part 的貼文留在原列並設不追蹤
    rename_dir: tuple[str, str] | None = None    # (舊目錄名, 新目錄名)


HENTAI_RULINGS = [
    Ruling(
        placeholder="sn:lativi_87251195_U149", action="heal", uid="87251195",
        why="目錄 AI_lativi_87251195_U149 的 uid 在中間，字尾是作品集名；"
            "使用者確認 87251195 就是 uid。真列 87251195 已存在 → 合併",
    ),
    Ruling(
        placeholder="sn:はやにぇR_39182623_エジプト娘", action="heal", uid="39182623",
        why="同上，尾巴掛的是作品集名。真列 39182623 已存在 → 合併",
    ),
    Ruling(
        placeholder="sn:むてきんぐ", action="heal", uid="53718392",
        why="使用者已把目錄改成 u_むてきんぐ_53718392。真列 53718392 已存在 → 合併",
        rename_dir=("u_むてきんぐ", "u_むてきんぐ_53718392"),
    ),
    Ruling(
        placeholder="sn:Ra_Lilium", action="split",
        why="u_Ra_Lilium 是容器目錄，底下是同一人的數個分身帳號，"
            "uid 在第二層 —— 匯入器只看第一層所以全部歸成一個哨符",
        parts={
            r"u_RA_21848":                 ("21848", "RA"),
            r"u_有江リリ_63051477":          ("63051477", "有江リリ"),
            r"u_LL_2001822":               ("2001822", "LL"),
            # `E` 是同人誌掃圖的雜物堆，但底下這個子目錄是貨真價實的 pixiv 作品
            r"E\[PIXIV] LL (2001822)":     ("2001822", "LL"),
            # `E` 其餘的 post_id 是假的（`20200403` 是檔名裡的日期、
            # `92713982` 來自桌布檔名）—— 不歸戶，留在原列並設不追蹤
        },
    ),
    Ruling(
        placeholder="sn:村田蓮爾", action="exclude",
        why="同人誌掃圖，post_id 是頁碼不是 pixiv 作品 id。使用者裁示排除",
    ),
]


# ────────────────────────────────── 目錄改名推導（純函式）

class AmbiguousRename(RuntimeError):
    """同一個舊目錄推導出兩種新前綴。**中止，不取多數決。**

    歧義代表「檔名+大小成對應」這個假設在這裡不成立，猜下去會把一批
    `local_path` 改到別人的目錄底下 —— 而那是不會報錯的靜默損壞。
    """


def iter_scan_rows(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("_meta") or "bytes" not in row:
                continue
            yield row


def derive_renames(old_rows, new_rows) -> dict[str, str]:
    """舊掃描 ↔ 新掃描 → `{舊頂層目錄: 新前綴}`（含沒變的）。

    鍵是 `(檔名, bytes)`：改名只動目錄，檔名與大小不變。只採用兩邊都唯一的
    鍵 —— 有碰撞的（同名同大小的檔案出現多次）沒辦法確定配對，直接不投票。

    新前綴可能不只一層：目錄被搬進 `_` 的話會是 `_/u_某人`。
    """
    def index(rows):
        d: dict[tuple[str, int], list[str]] = defaultdict(list)
        for r in rows:
            d[(r["name"], r["bytes"])].append(r["path"])
        return d

    old_ix, new_ix = index(old_rows), index(new_rows)
    votes: dict[str, Counter] = defaultdict(Counter)
    for key in old_ix.keys() & new_ix.keys():
        if len(old_ix[key]) != 1 or len(new_ix[key]) != 1:
            continue
        old_path, new_path = old_ix[key][0], new_ix[key][0]
        top, _, rest = old_path.partition("/")
        if not rest:
            continue                      # 根目錄下的散檔，沒有頂層目錄可映射
        if not new_path.endswith("/" + rest):
            # 目錄以下的相對路徑也變了 —— 那超出「只改頂層目錄名」的假設
            continue
        votes[top][new_path[: -(len(rest) + 1)]] += 1

    out = {}
    for top, counter in votes.items():
        if len(counter) > 1:
            raise AmbiguousRename(
                f"舊目錄 {top!r} 對應到多個新前綴：{dict(counter)}。"
                "這代表推導假設不成立，請人工確認後再跑。"
            )
        out[top] = next(iter(counter))
    return out


def uid_of_dir(dir_name: str) -> str | None:
    """`u_望月けい_1193008` → `1193008`。取不出來回 None，**不猜**。"""
    m = RE_PDIR.match(dir_name)
    rest = m["rest"] if m else dir_name
    um = RE_UID_TAIL.match(rest)
    return um["uid"] if um else None


def name_of_dir(dir_name: str) -> str:
    m = RE_PDIR.match(dir_name)
    rest = m["rest"] if m else dir_name
    um = RE_UID_TAIL.match(rest)
    return um["name"] if um else rest


# ────────────────────────────────── 報告

@dataclass
class Report:
    path_updates: int = 0
    renamed_dirs: list[tuple[str, str, int]] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    split: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    accounts_touched: set = field(default_factory=set)

    def print(self, committed: bool) -> None:
        head = "已寫入" if committed else "DRY-RUN（什麼都沒寫）"
        print("\n" + "=" * 78)
        print(f"### 修復報告 —— {head}")
        print("=" * 78)
        print(f"\n[1] 目錄改名 {len(self.renamed_dirs)} 個 → "
              f"local_path 更新 {self.path_updates:,} 筆")
        for old, new, n in sorted(self.renamed_dirs):
            if n:
                print(f"    {n:>6}  {old}  ->  {new}")
        zero = [(o, n) for o, n, c in self.renamed_dirs if not c]
        if zero:
            print(f"    （另有 {len(zero)} 個目錄改了名但 DB 沒有對應媒體，略過）")
        for title, items in (("[2] 換 id（原地）", self.healed),
                             ("[3] 合併進既有真列", self.merged),
                             ("[4] 依子目錄拆分", self.split),
                             ("[5] 設為不追蹤", self.excluded)):
            print(f"\n{title}：{len(items)} 筆")
            for s in items:
                print(f"    {s}")
        if self.notes:
            print("\n[!] 需要注意：")
            for s in self.notes:
                print(f"    {s}")


# ────────────────────────────────── 資料庫操作

class Repairer:
    def __init__(self, con: sqlite3.Connection, rep: Report):
        self.con = con
        self.rep = rep
        self.now = datetime.now(timezone.utc).isoformat(" ")

    # ── 讀 ──

    def media_under(self, root: str) -> list[tuple[int, str, int, int]]:
        """`(media_id, local_path, post_id, account_id)`，限 root 底下。

        一個 root 掃一次全表（`local_path` 沒有索引），分桶留給 Python 做 ——
        每個目錄各發一次 LIKE 的話是 50 次全表掃描。
        """
        pre = root.rstrip("\\") + "\\"
        return [
            r for r in self.con.execute(
                "SELECT m.id, m.local_path, p.id, p.account_id"
                " FROM media m JOIN posts p ON p.id = m.post_id"
                " WHERE m.local_path IS NOT NULL")
            if r[1].startswith(pre)
        ]

    def account(self, account_id: int):
        return self.con.execute(
            "SELECT id, platform, instance_host, platform_user_id, screen_name,"
            " is_tracked FROM accounts WHERE id = ?", (account_id,)).fetchone()

    def find_pixiv_account(self, user_id: str):
        return self.con.execute(
            "SELECT id, screen_name FROM accounts"
            " WHERE platform = 'pixiv' AND instance_host = ''"
            " AND platform_user_id = ?", (user_id,)).fetchone()

    # ── 寫 ──

    def rewrite_paths(self, updates: list[tuple[str, int]]) -> None:
        self.con.executemany("UPDATE media SET local_path = ? WHERE id = ?", updates)

    def ensure_account(self, uid: str, screen_name: str) -> int:
        row = self.find_pixiv_account(uid)
        if row:
            return row[0]
        cur = self.con.execute(
            "INSERT INTO accounts (platform, instance_host, platform_user_id,"
            " screen_name, is_tracked, created_at, is_favorite,"
            " post_count, media_count)"
            " VALUES ('pixiv', '', ?, ?, 1, ?, 0, 0, 0)",
            (uid, screen_name, self.now))
        self.rep.notes.append(f"新建帳號列 {uid}「{screen_name}」")
        return cur.lastrowid

    def heal(self, ghost_id: int, real_uid: str, why: str) -> int:
        """哨符列換成真 uid。真列已存在就合併，回留下來的那一列的 id。

        與 `services/identity.heal_placeholder_account` 同語意：
        貼文搬過去、偏好欄位只在真列沒設過時繼承、寫一筆 `identity_heals`。
        """
        ghost = self.account(ghost_id)
        _, platform, host, ph_id, screen, _ = ghost
        real = self.find_pixiv_account(real_uid)

        if real is None:
            self.con.execute(
                "UPDATE accounts SET platform_user_id = ? WHERE id = ?",
                (real_uid, ghost_id))
            self._record(platform, host, screen, ph_id, real_uid, "rename", 0)
            self.rep.healed.append(f"{ph_id}(id={ghost_id}) → {real_uid}｜{why}")
            self.rep.accounts_touched.add(ghost_id)
            return ghost_id

        real_id, real_name = real
        moved = self.con.execute(
            "UPDATE posts SET account_id = ? WHERE account_id = ?",
            (real_id, ghost_id)).rowcount
        self._inherit(ghost_id, real_id)
        self._record(platform, host, screen, ph_id, real_uid, "merge", moved)
        self.con.execute("DELETE FROM accounts WHERE id = ?", (ghost_id,))
        self.rep.merged.append(
            f"{ph_id}(id={ghost_id}) → {real_uid}「{real_name}」(id={real_id})"
            f"，搬 {moved} 則貼文｜{why}")
        self.rep.accounts_touched.add(real_id)
        return real_id

    def _inherit(self, ghost_id: int, real_id: int) -> None:
        """使用者手動標過的偏好不該因為合併而消失。真列沒設過的才繼承。"""
        cols = ", ".join(INHERIT_FIELDS)
        g = dict(zip(INHERIT_FIELDS, self.con.execute(
            f"SELECT {cols} FROM accounts WHERE id = ?", (ghost_id,)).fetchone()))
        r = dict(zip(INHERIT_FIELDS, self.con.execute(
            f"SELECT {cols} FROM accounts WHERE id = ?", (real_id,)).fetchone()))
        take = {k: g[k] for k in INHERIT_FIELDS if not r[k] and g[k]}
        if take:
            sets = ", ".join(f"{k} = ?" for k in take)
            self.con.execute(f"UPDATE accounts SET {sets} WHERE id = ?",
                             (*take.values(), real_id))

    def _record(self, platform, host, screen, ph_id, real_id, kind, moved) -> None:
        """把治療記下來。**log 會捲掉，這張表不會。**"""
        self.con.execute(
            "INSERT INTO identity_heals (platform, instance_host, screen_name,"
            " placeholder_id, real_id, kind, moved_posts, at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (platform, host or "", screen or "", ph_id, real_id, kind, moved, self.now))

    def untrack(self, account_id: int, label: str, why: str) -> None:
        """設為不追蹤。**已經是不追蹤就不回報** —— 重跑時報一堆「做了什麼」
        但其實什麼都沒改，會讓人以為腳本不是冪等的。"""
        row = self.con.execute("SELECT is_tracked FROM accounts WHERE id = ?",
                               (account_id,)).fetchone()
        if row is None or not row[0]:
            return
        self.con.execute("UPDATE accounts SET is_tracked = 0 WHERE id = ?",
                         (account_id,))
        self.rep.excluded.append(f"{label}(id={account_id})｜{why}")

    def recompute(self, account_ids) -> None:
        """聚合欄重算。定義照抄 `services/counters._exprs()`（含 preview_media）。"""
        ids = sorted({i for i in account_ids if i})
        for aid in ids:
            if not self.con.execute("SELECT 1 FROM accounts WHERE id = ?",
                                    (aid,)).fetchone():
                continue           # 合併時被刪掉的哨符列
            self.con.execute("""
                UPDATE accounts SET
                  post_count = (SELECT count(*) FROM posts WHERE account_id = ?),
                  media_count = (SELECT count(*) FROM media m JOIN posts p
                                  ON p.id = m.post_id WHERE p.account_id = ?),
                  last_post_at = (SELECT max(posted_at) FROM posts WHERE account_id = ?),
                  last_ingest_at = (SELECT max(ingested_at) FROM posts WHERE account_id = ?),
                  preview_media = (SELECT json_group_array(mid) FROM (
                      SELECT m.id AS mid FROM media m JOIN posts p ON p.id = m.post_id
                      WHERE p.account_id = ? ORDER BY p.posted_at DESC, m.id DESC
                      LIMIT 4))
                WHERE id = ?""", (aid, aid, aid, aid, aid, aid))


# ────────────────────────────────── 階段

def phase_paths(rp: Repairer, batches, scan_dir: Path) -> None:
    """階段一：目錄改名 → 改 `local_path`。

    **先改路徑再認帳號**：改完之後所有東西都在新位置，後面認帳號只認新前綴。
    這讓整支腳本可以重跑 —— 第二次跑時舊前綴匹配 0 筆，什麼都不會發生。
    """
    for b in batches:
        old = newest(scan_dir, b.old_glob)
        new = newest(scan_dir, b.new_glob)
        print(f"  {b.label}: {old.name}  ->  {new.name}")
        renames = derive_renames(iter_scan_rows(old), iter_scan_rows(new))
        changed = {o: n for o, n in renames.items() if o != n.replace("/", "\\")}

        rows = rp.media_under(b.root)
        pre = b.root.rstrip("\\") + "\\"
        buckets = defaultdict(list)
        for mid, lp, _pid, _aid in rows:
            top = lp[len(pre):].split("\\")[0]
            if top in changed:
                buckets[top].append((mid, lp))

        updates = []
        for old_top, new_prefix in changed.items():
            new_win = new_prefix.replace("/", "\\")
            for mid, lp in buckets.get(old_top, []):
                updates.append((pre + new_win + lp[len(pre) + len(old_top):], mid))
            rp.rep.renamed_dirs.append((old_top, new_win, len(buckets.get(old_top, []))))
        rp.rewrite_paths(updates)
        rp.rep.path_updates += len(updates)


def phase_identity(rp: Repairer, batches) -> None:
    """階段二：新目錄名解得出 uid 的哨符帳號 → 治好。"""
    for b in batches:
        pre = b.root.rstrip("\\") + "\\"
        # 目錄 → 底下有哪些帳號（以**現在的** local_path 為準，也就是新路徑）
        by_dir = defaultdict(set)
        for _mid, lp, _pid, aid in rp.media_under(b.root):
            by_dir[lp[len(pre):].split("\\")[0]].add(aid)

        for dir_name, account_ids in sorted(by_dir.items()):
            if dir_name == "_":
                # 使用者把「找不到或根本不是 pixiv」的帳號搬進 `_`。不追蹤它們。
                for aid in sorted(account_ids):
                    acc = rp.account(aid)
                    if acc and acc[5]:
                        rp.untrack(aid, acc[3], "使用者把目錄搬進 `_`，裁示忽略")
                continue
            uid = uid_of_dir(dir_name)
            for aid in sorted(account_ids):
                acc = rp.account(aid)
                # ⚠️ **必須確認 platform 是 pixiv。** pixiv 的藝術家目錄底下常掛
                # 一個 Twitter 轉存的子目錄，匯入器把那些歸成 `platform='x'` 的
                # 哨符帳號 —— 拿 pixiv 目錄名的 uid 去治它們會把 X 帳號的身分
                # 改成一個 pixiv user id，而且不會有任何錯誤訊息。
                if not acc or acc[1] != "pixiv":
                    continue
                if not acc[3].startswith(PLACEHOLDER_PREFIX):
                    continue
                if uid is None:
                    rp.rep.notes.append(
                        f"⚠ {acc[3]}(id={aid}) 仍是哨符，但目錄 {dir_name!r} "
                        "解不出 uid —— 未處理")
                    continue
                rp.heal(aid, uid, f"{b.label} 目錄 {dir_name}")


def phase_rulings(rp: Repairer) -> None:
    """階段三：I: 那批的使用者裁示。"""
    pre = HENTAI_ROOT.rstrip("\\") + "\\"
    rows = rp.media_under(HENTAI_ROOT)

    for rule in HENTAI_RULINGS:
        acc = rp.con.execute(
            "SELECT id, screen_name FROM accounts WHERE platform = 'pixiv'"
            " AND instance_host = '' AND platform_user_id = ?",
            (rule.placeholder,)).fetchone()
        if acc is None:
            continue                      # 已經處理過（重跑）
        aid, screen = acc

        if rule.rename_dir:
            old_dir, new_dir = rule.rename_dir
            ups = [(pre + new_dir + lp[len(pre) + len(old_dir):], mid)
                   for mid, lp, _p, a in rows
                   if a == aid and lp.startswith(pre + old_dir + "\\")]
            rp.rewrite_paths(ups)
            rp.rep.path_updates += len(ups)
            rp.rep.renamed_dirs.append((old_dir, new_dir, len(ups)))

        if rule.action == "heal":
            rp.heal(aid, rule.uid, rule.why)
        elif rule.action == "exclude":
            rp.untrack(aid, rule.placeholder, rule.why)
        elif rule.action == "split":
            _split(rp, rule, aid, screen, pre, rows)
        else:
            raise SystemExit(f"未知的 action {rule.action!r}")


def _split(rp: Repairer, rule: Ruling, ghost_id: int, screen: str,
           pre: str, rows) -> None:
    """依第二層目錄把一個哨符帳號的貼文拆給多個真帳號。

    ⚠️ 一則貼文的媒體橫跨兩個 part 就中止。那代表 part 的切法不對，
    硬拆會把同一則貼文的媒體記在不同帳號名下。
    """
    mine = [(mid, lp, pid) for mid, lp, pid, aid in rows if aid == ghost_id]
    if not mine:
        rp.rep.notes.append(f"⚠ {rule.placeholder} 底下沒有媒體，未拆分")
        return
    # 帳號目錄名不一定等於 screen_name（匯入時經過 `<prefix>_` 剝除），
    # 所以從實際路徑取，不要從名字兜。
    acct_dir = pre + mine[0][1][len(pre):].split("\\")[0] + "\\"

    post_target: dict[int, str] = {}
    leftovers = 0
    for _mid, lp, pid in mine:
        rest = lp[len(acct_dir):] if lp.startswith(acct_dir) else lp
        hit = next((p for p in sorted(rule.parts, key=len, reverse=True)
                    if rest.startswith(p + "\\")), None)
        if hit is None:
            leftovers += 1
            continue
        if post_target.setdefault(pid, hit) != hit:
            raise SystemExit(
                f"貼文 id={pid} 的媒體橫跨 {post_target[pid]!r} 與 {hit!r} —— "
                "part 的切法不對，中止")

    touched = set()
    for part, (uid, name) in rule.parts.items():
        pids = [p for p, h in post_target.items() if h == part]
        if not pids:
            continue
        target = rp.ensure_account(uid, name)
        rp.con.executemany("UPDATE posts SET account_id = ? WHERE id = ?",
                           [(target, p) for p in pids])
        rp.rep.split.append(
            f"{rule.placeholder}\\{part} → {uid}「{name}」(id={target})"
            f"，{len(pids)} 則貼文")
        touched.add(target)
    rp.rep.accounts_touched |= touched
    rp.rep.accounts_touched.add(ghost_id)

    if leftovers:
        rp.untrack(ghost_id, rule.placeholder,
                   f"拆分後剩 {leftovers} 筆媒體無法歸戶（post_id 是假的）"
                   f"，留在原列不追蹤｜{rule.why}")
    else:
        rp.con.execute("DELETE FROM accounts WHERE id = ?", (ghost_id,))
        rp.rep.notes.append(f"{rule.placeholder} 拆完已無貼文，刪除該列")


# ────────────────────────────────── 主流程

def newest(scan_dir: Path, pattern: str) -> Path:
    hits = sorted(scan_dir.glob(pattern))
    if not hits:
        raise SystemExit(f"{scan_dir} 底下找不到 {pattern}")
    return hits[-1]


def check_schema(con: sqlite3.Connection) -> None:
    try:
        con.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        raise SystemExit("目標 DB 沒有 alembic_version —— 這不是本專案的資料庫？")
    for table in ("accounts", "posts", "media", "identity_heals"):
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                           " AND name=?", (table,)).fetchone():
            raise SystemExit(f"目標 DB 少了 {table} 表。先跑 alembic upgrade head。")


def run(args) -> int:
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA foreign_keys = ON")
    check_schema(con)
    rep = Report()
    rp = Repairer(con, rep)

    before = con.execute("SELECT (SELECT count(*) FROM posts),"
                         " (SELECT count(*) FROM media)").fetchone()

    print("── 階段一：目錄改名 → local_path ──────────")
    phase_paths(rp, SCAN_BATCHES, args.scan_dir)
    print("\n── 階段二：哨符 → 真 uid ─────────────────")
    phase_identity(rp, SCAN_BATCHES)
    print("── 階段三：I: 那批的使用者裁示 ────────────")
    phase_rulings(rp)
    rp.recompute(rep.accounts_touched)

    after = con.execute("SELECT (SELECT count(*) FROM posts),"
                        " (SELECT count(*) FROM media)").fetchone()
    if before != after:
        con.rollback()
        raise SystemExit(f"⚠ 貼文/媒體總數變了 {before} -> {after} —— 已回滾。"
                         "這支腳本不該增刪任何一筆，出現這個代表有 bug。")

    left = con.execute(
        "SELECT count(*) FROM accounts WHERE platform = 'pixiv'"
        " AND platform_user_id LIKE 'sn:%' AND is_tracked = 1").fetchone()[0]
    if left:
        rep.notes.append(f"⚠ 還有 {left} 個 pixiv 哨符帳號在追蹤中 —— 一鍵更新仍會失敗")

    rep.print(committed=args.commit)
    print(f"\n貼文 {after[0]:,}｜媒體 {after[1]:,}（前後相同 ✓）")
    print(f"仍在追蹤的 pixiv 哨符帳號：{left}")

    if args.commit:
        # ⚠️ 報告已經印完了，接下來這一步**沒有任何輸出**。
        # 幾十萬列的 UPDATE 堆在 -wal 裡，commit 要 fsync 再 checkpoint 回主檔，
        # 在外接碟上可能是好幾分鐘。不先講的話，畫面看起來就是「印完報告後當掉」。
        print("\n寫入磁碟中（COMMIT + WAL checkpoint）—— 這一步沒有進度輸出，")
        print("大型資料庫可能要數分鐘，請不要中斷。", flush=True)
        con.commit()
        print("\n已 commit。")
    else:
        con.rollback()
        print("\nDRY-RUN，已回滾。確認無誤後**先備份 DB**再加 --commit。")
    con.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="pixiv 目錄補 uid 後修復資料庫。不碰任何媒體檔。")
    ap.add_argument("--db", required=True, type=Path, help="目標 snsmediadl.db")
    ap.add_argument("--scan-dir", type=Path,
                    default=Path(__file__).resolve().parent.parent
                    / "userBridge" / "fileScan")
    ap.add_argument("--commit", action="store_true",
                    help="真的寫入。不加這個就只是 dry-run")
    args = ap.parse_args()
    if not args.commit:
        print("*** DRY-RUN —— 不會寫入任何東西。要真的修請加 --commit ***\n")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
