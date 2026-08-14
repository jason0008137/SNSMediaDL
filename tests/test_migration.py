"""Alembic migration 必須與 models 產生一致的 schema，**而且不能弄丟資料**。

不一致的話，開發機（create_all）跟正式庫（alembic）會長出不同的表 ——
這種漂移通常等到資料寫壞了才會發現。

⚠️ 空 DB 上的 migration 測試有個天生的盲點：**沒有列可以出問題**。
`a1b2c3d4e5f6` 就是這樣溜過去的 —— 它在空 DB 上完美無瑕，在有資料的 DB 上
把 962 則貼文 cascade 刪光。所以本檔一定要有帶資料的那條測試。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from snsmediadl.db.models import Base

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_REVISION = "3fa63fbe0dd9"


def _alembic(db: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env={**__import__("os").environ, "SNSMEDIADL_DB_PATH": str(db)},
        capture_output=True,
        text=True,
    )


def _schema_snapshot(engine) -> dict:
    insp = inspect(engine)
    snap = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version":
            continue
        snap[table] = {
            "columns": sorted(c["name"] for c in insp.get_columns(table)),
            "unique": sorted(
                tuple(sorted(u["column_names"]))
                for u in insp.get_unique_constraints(table)
            ),
            "indexes": sorted(i["name"] for i in insp.get_indexes(table)),
        }
    return snap


@pytest.mark.slow
def test_alembic_matches_models(tmp_path):
    db = tmp_path / "migrated.db"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={
            **__import__("os").environ,
            "SNSMEDIADL_DB_PATH": str(db),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    migrated = _schema_snapshot(create_engine(f"sqlite:///{db}"))

    direct_db = tmp_path / "direct.db"
    direct_engine = create_engine(f"sqlite:///{direct_db}")
    Base.metadata.create_all(direct_engine)
    direct = _schema_snapshot(direct_engine)

    assert migrated == direct


@pytest.mark.slow
def test_migration_preserves_existing_rows(tmp_path):
    """帶著資料跑完整條 migration 鏈，一列都不能少。

    這是 `test_alembic_matches_models` 抓不到的一整類錯誤：schema 可以完全正確
    而資料被清空。實際發生過 —— batch 模式重建表時 `DROP TABLE` 在
    `PRAGMA foreign_keys=ON` 下會觸發 ON DELETE CASCADE，把子表整個帶走，
    而 migration 回報成功、沒有任何錯誤訊息。
    """
    db = tmp_path / "seeded.db"
    r = _alembic(db, "upgrade", BASE_REVISION)
    assert r.returncode == 0, r.stderr

    # 直接下 SQL 而不是用 models：models 是**現在**的形狀，這裡要的是
    # base revision 當下的形狀。用 models 會插不進去（多了 instance_host）。
    con = sqlite3.connect(db)
    con.executescript(
        """
        INSERT INTO accounts (id, platform, platform_user_id, screen_name,
                              is_tracked, created_at)
        VALUES (1, 'x', 'u1', 'someone', 1, '2026-01-01 00:00:00');

        INSERT INTO posts (id, platform, platform_post_id, account_id,
                           is_retweet, ingested_at)
        VALUES (1, 'x', 'p1', 1, 0, '2026-01-01 00:00:00'),
               (2, 'x', 'p2', 1, 0, '2026-01-01 00:00:00');

        INSERT INTO media (id, post_id, ordinal, kind, source_url, status,
                           attempt_count, local_path)
        VALUES (1, 1, 0, 'photo', 'https://example.invalid/a.jpg', 'done', 1,
                'downloads/x/someone/a.jpg'),
               (2, 2, 0, 'photo', 'https://example.invalid/b.jpg', 'done', 1,
                'downloads/x/someone/b.jpg');
        """
    )
    con.commit()
    con.close()

    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    con = sqlite3.connect(db)
    counts = {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("accounts", "posts", "media")
    }
    # local_path 沒了等於「不重抓」的判斷依據沒了，下次採集會整批重下載
    kept_paths = con.execute(
        "SELECT COUNT(*) FROM media WHERE local_path IS NOT NULL"
    ).fetchone()[0]
    violations = con.execute("PRAGMA foreign_key_check").fetchall()
    con.close()

    assert counts == {"accounts": 1, "posts": 2, "media": 2}, (
        f"migration 弄丟了資料：{counts}"
    )
    assert kept_paths == 2
    assert violations == [], f"migration 留下孤兒列：{violations}"


@pytest.mark.slow
def test_migrated_db_enforces_the_stars_check(tmp_path):
    """CHECK 是 schema 比對的盲區，只能用行為測。

    `_schema_snapshot` 比的是欄位 / 唯一鍵 / 索引 —— SQLAlchemy 的 SQLite
    dialect **不反射 CHECK constraint**，所以 migration 把約束漏掉的話，
    `test_alembic_matches_models` 一樣是綠的。要等到有人存了 `stars=99`
    才會發現，而那時候資料已經髒了。
    """
    db = tmp_path / "checks.db"
    r = _alembic(db, "upgrade", "head")
    assert r.returncode == 0, r.stderr

    con = sqlite3.connect(db)
    con.executescript(
        """
        INSERT INTO accounts (id, platform, instance_host, platform_user_id,
                              is_tracked, created_at, is_favorite)
        VALUES (1, 'x', '', 'u1', 1, '2026-01-01 00:00:00', 0);

        INSERT INTO posts (id, platform, instance_host, platform_post_id,
                           account_id, is_retweet, ingested_at)
        VALUES (1, 'x', '', 'p1', 1, 0, '2026-01-01 00:00:00');

        INSERT INTO media (id, post_id, ordinal, kind, source_url, status,
                           attempt_count)
        VALUES (1, 1, 0, 'photo', 'https://example.invalid/a.jpg', 'done', 0);
        """
    )

    # 0 也要被擋 —— 清除評分的表示法是 NULL，不是 0
    for table in ("media", "accounts"):
        for bad in (0, 6, -1):
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(f"UPDATE {table} SET stars = {bad}")

    con.execute("UPDATE media SET stars = 5")
    con.execute("UPDATE accounts SET stars = 1")
    con.execute("UPDATE media SET stars = NULL")
    con.commit()
    con.close()


def test_migration_creates_expected_tables(tmp_path):
    db = tmp_path / "m.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env={**__import__("os").environ, "SNSMEDIADL_DB_PATH": str(db)},
        capture_output=True,
        text=True,
        check=True,
    )
    names = set(inspect(create_engine(f"sqlite:///{db}")).get_table_names())
    assert {"creators", "accounts", "posts", "media"} <= names
