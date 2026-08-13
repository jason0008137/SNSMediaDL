"""執行期設定開關與批次標記。"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from snsmediadl.api.app import _download_loop, create_app, get_session
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


# ── 設定 ──────────────────────────────────────────────

def test_auto_download_defaults_off(client):
    """會自己對外發請求的東西，預設應該是關的。"""
    assert client.get("/api/settings").json()["auto_download"] is False


def test_toggle_auto_download(client):
    assert client.patch("/api/settings",
                        json={"auto_download": True}).json()["auto_download"] is True
    assert client.get("/api/settings").json()["auto_download"] is True

    assert client.patch("/api/settings",
                        json={"auto_download": False}).json()["auto_download"] is False


def test_settings_exposes_rate_limit_values(client, cfg):
    s = client.get("/api/settings").json()
    assert s["concurrency"] == cfg.concurrency
    assert s["download_delay_seconds"] == cfg.download_delay_seconds


async def test_download_loop_respects_the_flag(cfg, engine, session, sample_account):
    """關閉時迴圈不可下載 —— 這是開關唯一的意義。"""
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    ingest(session, "x", sample_account, screen_name="a")

    cfg = dataclasses.replace(cfg, auto_download=False, poll_interval_seconds=0.05)
    task = asyncio.create_task(_download_loop(cfg, maker))
    await asyncio.sleep(0.3)
    task.cancel()

    # 網路被 conftest 的 autouse fixture 擋著 —— 若迴圈真的動手下載，
    # 這些會變成 failed 而不是維持 pending。
    session.expire_all()
    assert {m.status for m in session.scalars(select(Media))} == {"pending"}


# ── 批次標記 ──────────────────────────────────────────

def _post_ids(client):
    return [p["id"] for p in client.get("/api/posts").json()["items"]]


def test_bulk_tag_updates_all(client, loaded):
    ids = _post_ids(client)
    r = client.post("/api/posts/bulk-tags",
                    json={"post_ids": ids, "rating": "r18"}).json()
    assert r["updated"] == 4
    assert all(p["rating"] == "r18" for p in client.get("/api/posts").json()["items"])


def test_bulk_tag_marks_manual(client, loaded):
    ids = _post_ids(client)
    client.post("/api/posts/bulk-tags", json={"post_ids": ids, "content_type": "ai"})
    assert all(p["rating_source"] == "manual"
               for p in client.get("/api/posts").json()["items"])


def test_bulk_tag_only_touches_provided_fields(client, loaded):
    ids = _post_ids(client)
    client.post("/api/posts/bulk-tags",
                json={"post_ids": ids, "rating": "r18", "content_type": "ai"})
    client.post("/api/posts/bulk-tags", json={"post_ids": ids, "rating": "sfw"})

    posts = client.get("/api/posts").json()["items"]
    assert all(p["rating"] == "sfw" for p in posts)
    assert all(p["content_type"] == "ai" for p in posts), "沒帶的欄位被洗掉了"


def test_bulk_tag_can_clear(client, loaded):
    ids = _post_ids(client)
    client.post("/api/posts/bulk-tags", json={"post_ids": ids, "rating": "r18"})
    client.post("/api/posts/bulk-tags", json={"post_ids": ids, "rating": None})
    assert all(p["rating"] is None for p in client.get("/api/posts").json()["items"])


def test_bulk_tag_deduplicates_requested_ids(client, loaded):
    """前端把媒體去重成貼文，但重複的 id 送進來也不該重複計數。"""
    ids = _post_ids(client)
    r = client.post("/api/posts/bulk-tags",
                    json={"post_ids": ids + ids, "rating": "sfw"}).json()
    assert r["updated"] == 4
    assert r["requested"] == 4


def test_bulk_tag_empty_is_noop(client, loaded):
    assert client.post("/api/posts/bulk-tags",
                       json={"post_ids": [], "rating": "r18"}).json()["updated"] == 0


def test_bulk_tag_rejects_bad_value(client, loaded):
    ids = _post_ids(client)
    assert client.post("/api/posts/bulk-tags",
                       json={"post_ids": ids, "rating": "nope"}).status_code == 422
