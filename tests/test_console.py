"""stats / errors / logs —— GUI 的「問題」頁靠這三個端點。

沒有它們的話，下載失敗只會表現成「數字對不上」，使用者不知道去哪查。
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from snsmediadl.api import logbuf
from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Media
from snsmediadl.services.ingest import ingest


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture()
def loaded(client, sample_account):
    client.post("/api/ingest", json={"platform": "x", "screenName": "sample_account",
                                     "posts": sample_account})


def _fail_one(session, error="HTTP 404"):
    m = session.scalar(select(Media))
    m.status = "failed"
    m.error = error
    m.attempt_count = 3
    session.commit()
    return m


def test_stats_summary(client, loaded):
    s = client.get("/api/stats").json()
    assert s["media_total"] == 6
    assert s["post_total"] == 4
    assert s["account_total"] == 1
    assert s["by_status"]["pending"] == 6
    assert s["by_rating"]["unrated"] == 4
    assert set(s["by_kind"]) == {"photo", "video", "animated_gif"}


def test_errors_empty_when_nothing_failed(client, loaded):
    assert client.get("/api/errors").json()["items"] == []


def test_errors_lists_failure_with_context(client, session, loaded):
    _fail_one(session, "HTTP 404")
    items = client.get("/api/errors").json()["items"]
    assert len(items) == 1
    e = items[0]
    assert e["error"] == "HTTP 404"
    assert e["attempt_count"] == 3
    assert e["screen_name"] == "sample_account"
    assert e["platform"] == "x"
    assert e["post_id"]


def test_retry_all_failed(client, session, loaded):
    _fail_one(session)
    r = client.post("/api/media/retry-failed").json()
    assert r["requeued"] == 1
    assert client.get("/api/errors").json()["items"] == []
    assert client.get("/api/queue/status").json()["pending"] == 6


def test_logs_capture_messages(client):
    logbuf.clear()
    logging.getLogger("snsmediadl").error("測試錯誤訊息")
    items = client.get("/api/logs").json()["items"]
    assert any("測試錯誤訊息" in r["message"] for r in items)
    assert items[0]["level"] == "ERROR"


def test_logs_filter_by_level(client):
    logbuf.clear()
    log = logging.getLogger("snsmediadl")
    log.info("一般訊息")
    log.error("嚴重訊息")
    errs = client.get("/api/logs?level=ERROR").json()["items"]
    assert len(errs) == 1
    assert errs[0]["message"] == "嚴重訊息"


def test_log_buffer_is_bounded(client):
    """ring buffer 不能無限長，否則長時間運行會吃光記憶體。"""
    logbuf.clear()
    log = logging.getLogger("snsmediadl")
    for i in range(logbuf.MAX_RECORDS + 50):
        log.info("訊息 %d", i)
    assert len(logbuf.records(limit=10_000)) == logbuf.MAX_RECORDS
