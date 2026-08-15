"""crash 之後的收拾：孤兒回收、WAL checkpoint、備份、完整性檢查。

這些機制的價值全在「壞掉的時候看得見」，所以測試重點是**有沒有講出來**，
不只是有沒有修好。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db import recovery
from snsmediadl.db.enums import MediaStatus
from snsmediadl.db.models import Media
from snsmediadl.db.session import make_engine


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _statuses(session) -> list[str]:
    return [m.status for m in session.query(Media).all()]


# ── 孤兒回收 ──────────────────────────────────────────────

def test_reclaim_puts_stuck_downloads_back_to_pending(client, session, sample_account):
    """卡在 downloading 的媒體會被 worker 完全忽略 —— 它只撿 pending。

    正式庫實測有 4 筆處於這個狀態：不會被下載、不會報錯、GUI 上看不見。
    """
    client.post("/api/ingest", json={"platform": "x", "posts": sample_account})
    stuck = session.query(Media).limit(3).all()
    for m in stuck:
        m.status = MediaStatus.DOWNLOADING.value
    session.commit()

    n = recovery.reclaim_orphan_downloads(session)

    assert n == 3
    assert MediaStatus.DOWNLOADING.value not in _statuses(session)


def test_reclaim_says_so_out_loud(client, session, sample_account, caplog):
    """⚠️ 靜默修正等於把線索清掉。

    每次啟動都回收上百筆，代表 worker 有別的問題 —— 而唯一會透露這件事的
    就是這行 warning。
    """
    client.post("/api/ingest", json={"platform": "x", "posts": sample_account})
    session.query(Media).first().status = MediaStatus.DOWNLOADING.value
    session.commit()

    with caplog.at_level(logging.WARNING, logger="snsmediadl"):
        recovery.reclaim_orphan_downloads(session)

    assert any("downloading" in r.message or "回收" in r.message
               for r in caplog.records), "回收動作必須留下 warning"


def test_reclaim_is_quiet_when_nothing_stuck(client, session, sample_account, caplog):
    """沒事就別吵。每次啟動都印一行「回收 0 筆」會讓真的有事那次被淹沒。"""
    client.post("/api/ingest", json={"platform": "x", "posts": sample_account})

    with caplog.at_level(logging.WARNING, logger="snsmediadl"):
        assert recovery.reclaim_orphan_downloads(session) == 0
    assert not [r for r in caplog.records if "回收" in r.message]


def test_reclaim_leaves_other_statuses_alone(client, session, sample_account):
    client.post("/api/ingest", json={"platform": "x", "posts": sample_account})
    rows = session.query(Media).all()
    rows[0].status = MediaStatus.DONE.value
    rows[1].status = MediaStatus.FAILED.value
    rows[2].status = MediaStatus.DOWNLOADING.value
    session.commit()

    recovery.reclaim_orphan_downloads(session)

    after = _statuses(session)
    assert MediaStatus.DONE.value in after
    assert MediaStatus.FAILED.value in after
    assert MediaStatus.DOWNLOADING.value not in after


def test_app_startup_reclaims(cfg, sample_account):
    """回收要真的掛在啟動流程上，不是一個沒人呼叫的函式。

    ⚠️ 這一條**刻意不覆寫 `get_session`**：預設的 `session` fixture 綁的是
    in-memory engine，而 `create_app` 用的是 `cfg.db_path` 的檔案 DB。
    覆寫的話資料寫進記憶體、回收查的是檔案，測試會假綠。
    """
    from sqlalchemy.orm import sessionmaker

    with TestClient(create_app(cfg)) as c:
        c.post("/api/ingest", json={"platform": "x", "posts": sample_account})

    file_maker = sessionmaker(bind=make_engine(cfg), future=True)
    with file_maker() as s:
        s.query(Media).first().status = MediaStatus.DOWNLOADING.value
        s.commit()

    with TestClient(create_app(cfg)):
        pass

    with file_maker() as s:
        assert MediaStatus.DOWNLOADING.value not in _statuses(s)


# ── 完整性與備份 ──────────────────────────────────────────

def test_quick_check_passes_on_healthy_db(cfg):
    assert recovery.quick_check(make_engine(cfg)) == "ok"


def test_backup_produces_a_readable_copy(cfg, sample_account):
    """備份必須是**能讀的資料庫**，不只是「有產生檔案」。

    同樣不覆寫 `get_session` —— 備份讀的是 `cfg.db_path`，資料得真的寫進那裡。
    """
    import sqlite3

    with TestClient(create_app(cfg)) as c:
        c.post("/api/ingest", json={"platform": "x", "posts": sample_account})

    engine = make_engine(cfg)
    target = recovery.backup(engine, cfg.db_path, tag="test")

    assert target.exists()
    assert "bak-test-" in target.name          # 標籤要看得出用途
    con = sqlite3.connect(str(target))
    assert con.execute("select count(*) from media").fetchone()[0] == 6
    con.close()


def test_backup_name_is_timestamped(cfg):
    a = recovery.backup_path(cfg.db_path, "premigrate")
    assert "bak-premigrate-" in a.name
    # 原檔名保留在前面，一眼看得出是哪個 DB 的備份
    assert a.name.startswith(cfg.db_path.name)


def test_checkpoint_wal_does_not_raise_on_healthy_db(cfg):
    """關閉流程不該因為收尾失敗而炸掉。"""
    recovery.checkpoint_wal(make_engine(cfg))
