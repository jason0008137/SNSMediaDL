"""`scripts/repair_pixiv_dirs.py` 的行為測試。

全部跑在 `tmp_path` 的合成 jsonl 與合成 DB 上。**不碰任何真實媒體目錄** ——
與匯入器同一條紀律：這支腳本的輸入只有掃描檔與 SQLite。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "repair_pixiv_dirs.py"


def _load():
    name = "repair_pixiv_dirs"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rp = _load()


def scan_row(path: str, nbytes: int) -> dict:
    return {"path": path, "name": path.rsplit("/", 1)[-1],
            "ext": "." + path.rsplit(".", 1)[-1], "bytes": nbytes}


# ─────────────────────────────────────────── 改名推導

def test_derive_renames_finds_appended_uid():
    old = [scan_row("u_望月けい/望月けい_1_p0.jpg", 10),
           scan_row("u_望月けい/望月けい_2_p0.jpg", 20)]
    new = [scan_row("u_望月けい_1193008/望月けい_1_p0.jpg", 10),
           scan_row("u_望月けい_1193008/望月けい_2_p0.jpg", 20)]
    assert rp.derive_renames(old, new) == {"u_望月けい": "u_望月けい_1193008"}


def test_derive_renames_tracks_move_into_underscore_dir():
    """搬進 `_` 的話新前綴有兩層，不是單純換個目錄名。"""
    old = [scan_row("u_白君凝/a.jpg", 10)]
    new = [scan_row("_/u_白君凝/a.jpg", 10)]
    assert rp.derive_renames(old, new) == {"u_白君凝": "_/u_白君凝"}


def test_derive_renames_reports_unchanged_dirs_too():
    old = [scan_row("u_zzz_18718489/a.jpg", 10)]
    assert rp.derive_renames(old, list(old)) == {"u_zzz_18718489": "u_zzz_18718489"}


def test_derive_renames_ignores_colliding_keys():
    """同名同大小出現兩次就配不出唯一對應，不投票（而不是亂配）。"""
    old = [scan_row("u_a/dup.jpg", 10), scan_row("u_b/dup.jpg", 10),
           scan_row("u_a/uniq.jpg", 99)]
    new = [scan_row("u_a_1/dup.jpg", 10), scan_row("u_b_2/dup.jpg", 10),
           scan_row("u_a_1/uniq.jpg", 99)]
    assert rp.derive_renames(old, new) == {"u_a": "u_a_1"}


def test_derive_renames_aborts_on_ambiguity():
    """同一個舊目錄推出兩種新前綴 → 中止。**不取多數決。**"""
    old = [scan_row("u_a/x.jpg", 1), scan_row("u_a/y.jpg", 2),
           scan_row("u_a/z.jpg", 3)]
    new = [scan_row("u_a_1/x.jpg", 1), scan_row("u_a_1/y.jpg", 2),
           scan_row("u_a_2/z.jpg", 3)]
    with pytest.raises(rp.AmbiguousRename):
        rp.derive_renames(old, new)


@pytest.mark.parametrize("dir_name,uid,name", [
    ("u_望月けい_1193008", "1193008", "望月けい"),
    ("u_5ma.(ごま)_28478367", "28478367", "5ma.(ごま)"),
    ("AI_lativi_87251195", "87251195", "lativi"),
    ("u_零", None, "零"),
    ("_", None, "_"),
])
def test_uid_and_name_of_dir(dir_name, uid, name):
    assert rp.uid_of_dir(dir_name) == uid
    assert rp.name_of_dir(dir_name) == name


def test_uid_not_at_tail_is_not_guessed():
    """`AI_lativi_87251195_U149` 的 uid 在中間 —— 解析器不猜，回 None。

    這正是 I: 那批要靠使用者裁示（`HENTAI_RULINGS`）而不是自動推導的原因。
    """
    assert rp.uid_of_dir("AI_lativi_87251195_U149") is None


# ─────────────────────────────────────────── 端對端

SCHEMA = """
CREATE TABLE alembic_version (version_num TEXT);
CREATE TABLE accounts (
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, instance_host TEXT NOT NULL DEFAULT '',
  platform_user_id TEXT NOT NULL, screen_name TEXT, is_tracked BOOLEAN NOT NULL,
  created_at TEXT NOT NULL, creator_id INTEGER, role TEXT, default_rating TEXT,
  default_content_type TEXT, is_favorite BOOLEAN NOT NULL DEFAULT 0, stars INTEGER,
  post_count INTEGER NOT NULL DEFAULT 0, media_count INTEGER NOT NULL DEFAULT 0,
  last_post_at TEXT, last_ingest_at TEXT, preview_media TEXT,
  UNIQUE (platform, instance_host, platform_user_id));
CREATE TABLE posts (
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, instance_host TEXT NOT NULL DEFAULT '',
  platform_post_id TEXT NOT NULL, account_id INTEGER NOT NULL, posted_at TEXT,
  is_retweet BOOLEAN NOT NULL DEFAULT 0, ingested_at TEXT NOT NULL,
  UNIQUE (platform, instance_host, platform_post_id));
CREATE TABLE media (
  id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, ordinal INTEGER NOT NULL,
  kind TEXT NOT NULL, source_url TEXT NOT NULL, local_path TEXT, file_hash TEXT,
  bytes INTEGER, status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
  downloaded_at TEXT, UNIQUE (post_id, ordinal));
CREATE TABLE identity_heals (
  id INTEGER PRIMARY KEY, platform TEXT NOT NULL, instance_host TEXT NOT NULL DEFAULT '',
  screen_name TEXT NOT NULL, placeholder_id TEXT NOT NULL, real_id TEXT NOT NULL,
  kind TEXT NOT NULL, moved_posts INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL);
"""

ROOT1 = r"F:\Data\Illasuto\_Pixiv_1"


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO alembic_version VALUES ('test')")
    con.commit()
    con.close()
    return p


def add_account(con, platform, user_id, name, tracked=1, **extra):
    cols = "platform, instance_host, platform_user_id, screen_name, is_tracked, created_at"
    vals = [platform, "", user_id, name, tracked, "2026-01-01"]
    for k, v in extra.items():
        cols += f", {k}"
        vals.append(v)
    ph = ",".join("?" * len(vals))
    return con.execute(f"INSERT INTO accounts ({cols}) VALUES ({ph})", vals).lastrowid


def add_media(con, account_id, post_key, local_path, platform="pixiv", ordinal=0):
    row = con.execute("SELECT id FROM posts WHERE platform=? AND platform_post_id=?",
                      (platform, post_key)).fetchone()
    pid = row[0] if row else con.execute(
        "INSERT INTO posts (platform, instance_host, platform_post_id, account_id,"
        " ingested_at) VALUES (?,'',?,?,'2026-01-01')",
        (platform, post_key, account_id)).lastrowid
    con.execute("INSERT INTO media (post_id, ordinal, kind, source_url, local_path,"
                " status) VALUES (?,?,'photo','',?,'done')", (pid, ordinal, local_path))
    return pid


def write_scans(tmp_path, old_paths, new_paths):
    d = tmp_path / "scan"
    d.mkdir(exist_ok=True)
    for fname, paths in (("scan_PIXIV1_20260101-000000.jsonl", old_paths),
                         ("scan_PIXIV1_FIX_20260102-000000.jsonl", new_paths),
                         ("scan_PIXIV2_20260101-000000.jsonl", []),
                         ("scan_PIXIV2_FIX_20260102-000000.jsonl", [])):
        with (d / fname).open("w", encoding="utf-8") as f:
            f.write(json.dumps({"_meta": True}) + "\n")
            for i, p in enumerate(paths):
                f.write(json.dumps(scan_row(p, 100 + i), ensure_ascii=False) + "\n")
    return d


def run(db_path, scan_dir, commit=True):
    """只跑 PIXIV1 一批，並且關掉 I: 的裁示（那批另外測）。"""
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    rep = rp.Report()
    r = rp.Repairer(con, rep)
    batches = [b for b in rp.SCAN_BATCHES if b.label == "PIXIV1"]
    rp.phase_paths(r, batches, scan_dir)
    rp.phase_identity(r, batches)
    r.recompute(rep.accounts_touched)
    con.commit() if commit else con.rollback()
    return con, rep


def test_rename_rewrites_paths_and_heals_identity(db, tmp_path):
    con = sqlite3.connect(db)
    a = add_account(con, "pixiv", "sn:望月けい", "望月けい")
    add_media(con, a, "22696960", rf"{ROOT1}\u_望月けい\望月けい_22696960_p0.jpg")
    add_media(con, a, "22696961", rf"{ROOT1}\u_望月けい\望月けい_22696961_p0.jpg")
    con.commit()
    con.close()

    scan = write_scans(
        tmp_path,
        ["u_望月けい/望月けい_22696960_p0.jpg", "u_望月けい/望月けい_22696961_p0.jpg"],
        ["u_望月けい_1193008/望月けい_22696960_p0.jpg",
         "u_望月けい_1193008/望月けい_22696961_p0.jpg"])
    con, rep = run(db, scan)

    assert rep.path_updates == 2
    paths = [r[0] for r in con.execute("SELECT local_path FROM media")]
    assert all(p.startswith(rf"{ROOT1}\u_望月けい_1193008" + "\\") for p in paths)
    assert con.execute("SELECT platform_user_id FROM accounts WHERE id=?",
                       (a,)).fetchone()[0] == "1193008"
    # 聚合欄要跟著重算
    assert con.execute("SELECT media_count FROM accounts WHERE id=?",
                       (a,)).fetchone()[0] == 2
    kind = con.execute("SELECT kind, moved_posts FROM identity_heals").fetchone()
    assert kind == ("rename", 0)


def test_merge_when_real_row_already_exists(db, tmp_path):
    """`sn:東西`(哨符) 與 `16347608`(真列) 並存 —— 貼文搬過去、哨符列刪掉。"""
    con = sqlite3.connect(db)
    ghost = add_account(con, "pixiv", "sn:東西", "東西", is_favorite=1)
    real = add_account(con, "pixiv", "16347608", "東西")
    add_media(con, ghost, "57145848", rf"{ROOT1}\u_東西\東西_57145848_p0.jpg")
    add_media(con, real, "99999999", rf"{ROOT1}\u_東西_16347608\東西_99999999_p0.jpg")
    con.commit()
    con.close()

    scan = write_scans(
        tmp_path,
        ["u_東西/東西_57145848_p0.jpg", "u_東西_16347608/東西_99999999_p0.jpg"],
        ["u_東西_16347608/東西_57145848_p0.jpg",
         "u_東西_16347608/東西_99999999_p0.jpg"])
    con, rep = run(db, scan)

    assert con.execute("SELECT count(*) FROM accounts WHERE id=?", (ghost,)).fetchone()[0] == 0
    assert con.execute("SELECT media_count FROM accounts WHERE id=?", (real,)).fetchone()[0] == 2
    # 哨符列上的偏好要繼承過來，不能因為合併而消失
    assert con.execute("SELECT is_favorite FROM accounts WHERE id=?", (real,)).fetchone()[0] == 1
    assert con.execute("SELECT kind, moved_posts FROM identity_heals").fetchone() == ("merge", 1)


def test_moved_into_underscore_dir_is_untracked(db, tmp_path):
    con = sqlite3.connect(db)
    a = add_account(con, "pixiv", "sn:白君凝", "白君凝")
    add_media(con, a, "1", rf"{ROOT1}\u_白君凝\a.jpg")
    con.commit()
    con.close()

    scan = write_scans(tmp_path, ["u_白君凝/a.jpg"], ["_/u_白君凝/a.jpg"])
    con, rep = run(db, scan)

    assert con.execute("SELECT local_path FROM media").fetchone()[0] == \
        rf"{ROOT1}\_\u_白君凝\a.jpg"
    assert con.execute("SELECT is_tracked FROM accounts WHERE id=?", (a,)).fetchone()[0] == 0
    assert con.execute("SELECT platform_user_id FROM accounts WHERE id=?",
                       (a,)).fetchone()[0] == "sn:白君凝"      # 沒有 uid 就不編一個


def test_x_account_nested_in_pixiv_dir_is_not_healed_to_pixiv_uid(db, tmp_path):
    """pixiv 藝術家目錄底下常掛 Twitter 轉存子目錄，那些是 `platform='x'` 的哨符。

    拿 pixiv 目錄名的 uid 去治它們，會把 X 帳號的身分改成一個 pixiv user id，
    而且不會有任何錯誤訊息。
    """
    con = sqlite3.connect(db)
    px = add_account(con, "pixiv", "sn:れおえん", "れおえん")
    tw = add_account(con, "x", "sn:reoenl", "reoenl")
    add_media(con, px, "1", rf"{ROOT1}\u_れおえん\れおえん_1_p0.jpg")
    add_media(con, tw, "1521803469929467912",
              rf"{ROOT1}\u_れおえん\れおえんTwitter\reoenl-1521803469929467912-x-vid1.mp4",
              platform="x")
    con.commit()
    con.close()

    scan = write_scans(
        tmp_path,
        ["u_れおえん/れおえん_1_p0.jpg",
         "u_れおえん/れおえんTwitter/reoenl-1521803469929467912-x-vid1.mp4"],
        ["u_れおえん_3927625/れおえん_1_p0.jpg",
         "u_れおえん_3927625/れおえんTwitter/reoenl-1521803469929467912-x-vid1.mp4"])
    con, rep = run(db, scan)

    assert con.execute("SELECT platform_user_id FROM accounts WHERE id=?",
                       (px,)).fetchone()[0] == "3927625"
    assert con.execute("SELECT platform_user_id FROM accounts WHERE id=?",
                       (tw,)).fetchone()[0] == "sn:reoenl"     # 沒被動到
    # 但兩邊的路徑都要跟著改名
    assert all(r"u_れおえん_3927625" in p
               for (p,) in con.execute("SELECT local_path FROM media"))


def test_rerun_is_idempotent(db, tmp_path):
    con = sqlite3.connect(db)
    a = add_account(con, "pixiv", "sn:零", "零")
    add_media(con, a, "1072239", rf"{ROOT1}\u_零\零_1072239_p0.jpg")
    con.commit()
    con.close()

    scan = write_scans(tmp_path, ["u_零/零_1072239_p0.jpg"],
                       ["u_零_74184/零_1072239_p0.jpg"])
    con, _ = run(db, scan)
    first = con.execute("SELECT local_path FROM media").fetchone()[0]
    con.close()

    con, rep2 = run(db, scan)
    assert rep2.path_updates == 0
    assert rep2.healed == [] and rep2.merged == []
    assert con.execute("SELECT local_path FROM media").fetchone()[0] == first
    assert con.execute("SELECT count(*) FROM identity_heals").fetchone()[0] == 1


# ─────────────────────────────────────────── I: 那批的裁示

def test_rulings_cover_every_documented_case():
    """裁示表是使用者逐帳號拍板的結果，動它要有新的裁示。"""
    by_ph = {r.placeholder: r for r in rp.HENTAI_RULINGS}
    assert by_ph["sn:lativi_87251195_U149"].action == "heal"
    assert by_ph["sn:lativi_87251195_U149"].uid == "87251195"
    assert by_ph["sn:はやにぇR_39182623_エジプト娘"].uid == "39182623"
    assert by_ph["sn:むてきんぐ"].rename_dir == ("u_むてきんぐ", "u_むてきんぐ_53718392")
    assert by_ph["sn:村田蓮爾"].action == "exclude"
    assert set(by_ph["sn:Ra_Lilium"].parts.values()) == {
        ("21848", "RA"), ("63051477", "有江リリ"), ("2001822", "LL")}
    assert all(r.why for r in rp.HENTAI_RULINGS)


def test_split_reassigns_posts_and_untracks_leftovers(db):
    """`u_Ra_Lilium` 底下是同一人的數個分身，uid 在第二層目錄。"""
    H = rp.HENTAI_ROOT
    con = sqlite3.connect(db)
    ghost = add_account(con, "pixiv", "sn:Ra_Lilium", "Ra_Lilium")
    add_media(con, ghost, "100087044", rf"{H}\u_Ra_Lilium\u_有江リリ_63051477\a_100087044_p0.jpg")
    add_media(con, ghost, "48088156", rf"{H}\u_Ra_Lilium\u_RA_21848\b_48088156_p0.jpg")
    add_media(con, ghost, "51420932",
              rf"{H}\u_Ra_Lilium\E\[PIXIV] LL (2001822)\2001822_51420932_p0.jpg")
    # post_id 是檔名裡的日期 —— 假 id，不歸戶
    add_media(con, ghost, "20200403",
              rf"{H}\u_Ra_Lilium\E\Artist LL\05_80544894_p0_20200403.jpg")
    con.commit()

    rep = rp.Report()
    r = rp.Repairer(con, rep)
    rp.phase_rulings(r)
    r.recompute(rep.accounts_touched)
    con.commit()

    def acc(uid):
        return con.execute("SELECT id, media_count FROM accounts"
                           " WHERE platform='pixiv' AND platform_user_id=?", (uid,)).fetchone()
    assert acc("63051477")[1] == 1
    assert acc("21848")[1] == 1
    assert acc("2001822")[1] == 1
    # 歸不了戶的留在原列，設不追蹤 —— 不刪記錄
    left = con.execute("SELECT is_tracked, media_count FROM accounts WHERE id=?",
                       (ghost,)).fetchone()
    assert left == (0, 1)
    assert con.execute("SELECT count(*) FROM media").fetchone()[0] == 4


def test_split_aborts_when_a_post_straddles_two_parts(db):
    H = rp.HENTAI_ROOT
    con = sqlite3.connect(db)
    ghost = add_account(con, "pixiv", "sn:Ra_Lilium", "Ra_Lilium")
    add_media(con, ghost, "555", rf"{H}\u_Ra_Lilium\u_RA_21848\x_555_p0.jpg", ordinal=0)
    add_media(con, ghost, "555", rf"{H}\u_Ra_Lilium\u_LL_2001822\x_555_p1.jpg", ordinal=1)
    con.commit()

    r = rp.Repairer(con, rp.Report())
    with pytest.raises(SystemExit, match="橫跨"):
        rp.phase_rulings(r)


def test_rulings_rerun_reports_nothing(db):
    """裁示重跑要安靜。回報一堆「做了什麼」但其實沒改，會讓人誤判成不冪等。"""
    H = rp.HENTAI_ROOT
    con = sqlite3.connect(db)
    ghost = add_account(con, "pixiv", "sn:Ra_Lilium", "Ra_Lilium")
    add_media(con, ghost, "48088156", rf"{H}\u_Ra_Lilium\u_RA_21848\b_48088156_p0.jpg")
    add_media(con, ghost, "20200403", rf"{H}\u_Ra_Lilium\E\x\05_p0_20200403.jpg")
    a = add_account(con, "pixiv", "sn:村田蓮爾", "村田蓮爾")
    add_media(con, a, "10126", rf"{H}\u_村田蓮爾\d\001_last_exile_10126.jpg")
    con.commit()

    rp.phase_rulings(rp.Repairer(con, rp.Report()))
    con.commit()
    rep2 = rp.Report()
    rp.phase_rulings(rp.Repairer(con, rep2))
    assert rep2.excluded == [] and rep2.split == [] and rep2.merged == []


def test_exclude_keeps_records_and_only_untracks(db):
    H = rp.HENTAI_ROOT
    con = sqlite3.connect(db)
    a = add_account(con, "pixiv", "sn:村田蓮爾", "村田蓮爾")
    add_media(con, a, "10126", rf"{H}\u_村田蓮爾\doujin\001_last_exile_10126.jpg")
    con.commit()

    r = rp.Repairer(con, rp.Report())
    rp.phase_rulings(r)
    con.commit()

    assert con.execute("SELECT is_tracked FROM accounts WHERE id=?", (a,)).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM media").fetchone()[0] == 1


def test_ruling_rename_dir_moves_paths_then_merges(db):
    """`u_むてきんぐ` → `u_むてきんぐ_53718392`，真列已存在 → 合併。"""
    H = rp.HENTAI_ROOT
    con = sqlite3.connect(db)
    ghost = add_account(con, "pixiv", "sn:むてきんぐ", "むてきんぐ")
    real = add_account(con, "pixiv", "53718392", "むてき")
    add_media(con, ghost, "92473072", rf"{H}\u_むてきんぐ\むてきんぐ_92473072_p0.jpg")
    con.commit()

    rep = rp.Report()
    r = rp.Repairer(con, rep)
    rp.phase_rulings(r)
    r.recompute(rep.accounts_touched)
    con.commit()

    assert con.execute("SELECT local_path FROM media").fetchone()[0] == \
        rf"{H}\u_むてきんぐ_53718392\むてきんぐ_92473072_p0.jpg"
    assert con.execute("SELECT count(*) FROM accounts WHERE id=?", (ghost,)).fetchone()[0] == 0
    assert con.execute("SELECT media_count FROM accounts WHERE id=?", (real,)).fetchone()[0] == 1
