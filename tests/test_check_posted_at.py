"""`snsmediadl check-posted-at`：反正規化的一致性檢查。

⭐ 為什麼這支工具必須存在：`media.posted_at` 是 `posts.posted_at` 的副本，
它會漂移。而漂移的症狀是「排序看起來怪怪的」—— **沒有人認得出那是資料不一致**。
沒有工具可查的反正規化，等於把一個查不出來的 bug 種進去。

用子行程跑真的 CLI（不是直接呼叫函式）：exit code 也是介面的一部分，
它決定這支能不能被排進自動檢查。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run(db: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "snsmediadl.cli", *args],
        cwd=PROJECT_ROOT,
        env={**os.environ, "SNSMEDIADL_DB_PATH": str(db), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _alembic(db: Path) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**os.environ, "SNSMEDIADL_DB_PATH": str(db), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert r.returncode == 0, r.stderr


@pytest.fixture()
def db(tmp_path) -> Path:
    path = tmp_path / "check.db"
    _alembic(path)
    con = sqlite3.connect(path)
    con.executescript(
        """
        INSERT INTO accounts (id, platform, instance_host, platform_user_id,
                              screen_name, is_tracked, created_at)
        VALUES (1, 'x', '', 'u1', 'someone', 1, '2026-01-01 00:00:00');

        INSERT INTO posts (id, platform, instance_host, platform_post_id,
                           account_id, posted_at, is_retweet, ingested_at)
        VALUES (1, 'x', '', 'p1', 1, '2026-01-05 10:00:00', 0, '2026-01-01 00:00:00'),
               (2, 'x', '', 'p2', 1, NULL,                  0, '2026-01-01 00:00:00');

        INSERT INTO media (id, post_id, ordinal, kind, source_url, status,
                           attempt_count, posted_at)
        VALUES (1, 1, 0, 'photo', 'https://e.invalid/a', 'done', 1,
                '2026-01-05 10:00:00'),
               (2, 2, 0, 'photo', 'https://e.invalid/b', 'done', 1, NULL);
        """
    )
    con.commit()
    con.close()
    return path


@pytest.mark.slow
def test_clean_db_exits_zero(db):
    r = _run(db, "check-posted-at")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "一致" in r.stdout


@pytest.mark.slow
def test_drift_is_detected_and_exits_nonzero(db):
    """⭐ 故意改壞一筆，工具要抓得到。

    非 0 的 exit code 是重點 —— 這樣它才能被排進自動檢查，
    而不是靠人記得去看輸出。
    """
    con = sqlite3.connect(db)
    con.execute("UPDATE media SET posted_at = '1999-01-01 00:00:00' WHERE id = 1")
    con.commit()
    con.close()

    r = _run(db, "check-posted-at")
    assert r.returncode == 1
    assert "media#1" in r.stdout
    assert "什麼都沒改" in r.stdout


@pytest.mark.slow
def test_null_versus_value_counts_as_drift(db):
    """⭐ 一邊 NULL 一邊有值也是不一致。

    用 `!=` 比較的話這一種抓不到（SQL 的 `NULL != x` 是 NULL 不是 true），
    而它正是最需要抓到的：漏寫 posted_at 的那條路徑留下的就是 NULL。
    """
    con = sqlite3.connect(db)
    con.execute("UPDATE media SET posted_at = NULL WHERE id = 1")
    con.commit()
    con.close()

    r = _run(db, "check-posted-at")
    assert r.returncode == 1
    assert "media#1" in r.stdout


@pytest.mark.slow
def test_fix_writes_the_real_value_back(db):
    con = sqlite3.connect(db)
    con.execute("UPDATE media SET posted_at = NULL WHERE id = 1")
    con.commit()
    con.close()

    r = _run(db, "check-posted-at", "--fix")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "已修正 1 筆" in r.stdout

    con = sqlite3.connect(db)
    got = con.execute("SELECT posted_at FROM media WHERE id = 1").fetchone()[0]
    con.close()
    assert got == "2026-01-05 10:00:00"

    assert _run(db, "check-posted-at").returncode == 0
