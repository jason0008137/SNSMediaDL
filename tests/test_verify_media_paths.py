r"""`scripts/verify_media_paths.py`。

重點只有三件事：唯讀、長路徑不會被誤報成遺失、`--drive` 真的有篩。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from snsmediadl.fspath import for_io

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_media_paths.py"


@pytest.fixture()
def mod():
    spec = importlib.util.spec_from_file_location("verify_media_paths", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _db(tmp_path: Path, paths: list[str]) -> Path:
    """只建這支腳本真正會讀的欄位 —— 不用整套 schema。"""
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE media (id INTEGER PRIMARY KEY, local_path TEXT)")
    con.executemany("INSERT INTO media (local_path) VALUES (?)",
                    [(p,) for p in paths])
    con.commit()
    con.close()
    return db


def test_counts_present_and_missing(mod, tmp_path):
    here = tmp_path / "a.jpg"
    here.write_bytes(b"x")
    gone = tmp_path / "gone.jpg"

    report = mod.verify(_db(tmp_path, [str(here), str(gone)]), None)
    assert (report["ok"], report["missing"]) == (1, 1)
    assert report["missing_samples"][0]["local_path"] == str(gone)


def test_does_not_write_to_the_db(mod, tmp_path):
    """唯讀不是註解上的承諾 —— 連 mtime 都不該動。"""
    here = tmp_path / "a.jpg"
    here.write_bytes(b"x")
    db = _db(tmp_path, [str(here)])
    before = db.stat().st_mtime_ns

    mod.verify(db, None)
    assert db.stat().st_mtime_ns == before


def test_null_paths_are_not_counted(mod, tmp_path):
    """還沒下載的媒體 `local_path` 是 NULL，那不是「檔案不見了」。"""
    db = _db(tmp_path, [])
    con = sqlite3.connect(db)
    con.execute("INSERT INTO media (local_path) VALUES (NULL)")
    con.commit()
    con.close()

    report = mod.verify(db, None)
    assert report["rows_with_path"] == 0
    assert report["checked"] == 0


@pytest.mark.skipif(os.name != "nt", reason="磁碟機代號是 Windows 的事")
def test_drive_filter_skips_the_others(mod, tmp_path):
    here = tmp_path / "a.jpg"
    here.write_bytes(b"x")
    other = "Z:\\somewhere\\b.jpg"

    report = mod.verify(_db(tmp_path, [str(here), other]), "Z")
    assert report["skipped_by_filter"] == 1
    assert report["checked"] == 1
    assert report["by_drive"]["Z:"]["missing"] == 1


@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH 是 Windows 的事")
def test_long_path_that_exists_is_not_reported_missing(mod, tmp_path):
    r"""⭐ 這支腳本要是自己忘了 `\\?\`，報告會把好好的檔案列成遺失 ——
    而使用者接下來就會照那份名單去「修」一批根本沒壞的記錄。"""
    deep = tmp_path
    while len(str(deep)) < 240:
        deep = deep / ("d" * 40)
    target = deep / ("n" * 20 + ".jpg")
    assert len(str(target)) > 260

    for_io(deep).mkdir(parents=True, exist_ok=True)
    for_io(target).write_bytes(b"x")
    assert not target.exists()      # 沒有前綴時「不存在」

    report = mod.verify(_db(tmp_path, [str(target)]), None)
    assert report["missing"] == 0
    assert report["long_paths"]["ok"] == 1


@pytest.mark.skipif(os.name != "nt", reason="磁碟機代號是 Windows 的事")
def test_drive_of_handles_unc_and_letters(mod):
    assert mod.drive_of("K:\\a\\b.jpg") == "K:"
    assert mod.drive_of("\\\\nas\\media\\a.jpg") == "\\\\nas\\media"
    assert mod.drive_of("relative\\a.jpg") == "?"


def test_report_is_written_as_json(mod, tmp_path, monkeypatch, capsys):
    here = tmp_path / "a.jpg"
    here.write_bytes(b"x")
    db = _db(tmp_path, [str(here)])
    out = tmp_path / "reports"
    out.mkdir()

    monkeypatch.setattr("sys.argv",
                        ["verify_media_paths.py", "--db", str(db), "--out", str(out)])
    assert mod.main() == 0

    written = list(out.glob("verify_paths_*.json"))
    assert len(written) == 1
    data = json.loads(written[0].read_text(encoding="utf-8"))
    assert data["ok"] == 1
