"""抓取服務：分頁、增量停止、instance 隔離、認證錯誤。全部走 MockTransport。"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from snsmediadl.adapters.mastodon import MastodonAuthRequired
from snsmediadl.api.app import create_app
from snsmediadl.db.models import Account, Media, Post
from snsmediadl.services.fetch import fetch_account


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def cfg_fast(cfg):
    return dataclasses.replace(cfg, fetch_delay_seconds=0.0, fetch_max_pages=5)


def _msk_note(i: int, user="u1"):
    return {
        "id": f"note{i:03d}",
        "createdAt": "2026-08-01T10:00:00.000Z",
        "text": "x",
        "user": {"id": user, "username": "artist"},
        "files": [{"id": f"f{i}", "type": "image/png",
                   "url": f"https://files.misskey.io/{i}.png"}],
    }


def _misskey_server(pages: list[list[dict]]):
    """每次 /api/users/notes 回下一頁。記錄收到的 untilId 以驗證分頁。"""
    seen_cursors: list[str | None] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/users/show":
            return httpx.Response(200, json={"id": "u1", "username": "artist"})
        if request.url.path == "/api/users/notes":
            body = json.loads(request.content)
            seen_cursors.append(body.get("untilId"))
            idx = calls["n"]
            calls["n"] += 1
            return httpx.Response(200, json=pages[idx] if idx < len(pages) else [])
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen_cursors


async def test_fetch_walks_pages_until_exhausted(cfg_fast, maker):
    pages = [[_msk_note(1), _msk_note(2)], [_msk_note(3)], []]
    transport, cursors = _misskey_server(pages)

    r = await fetch_account(cfg_fast, maker, platform="misskey",
                            host="misskey.io", acct="@artist", transport=transport)

    assert r.posts_new == 3
    assert r.media_new == 3
    assert r.pages == 3
    assert r.stopped_because == "沒有下一頁了"
    # 第一頁不帶游標，之後帶「上一頁最後一則」的 id
    assert cursors == [None, "note002", "note003"]


async def test_fetch_is_incremental_by_default(cfg_fast, maker):
    """重跑不該重抓 —— 這是預設行為不是選項。"""
    transport, _ = _misskey_server([[_msk_note(1), _msk_note(2)], []])
    await fetch_account(cfg_fast, maker, platform="misskey", host="misskey.io",
                        acct="artist", transport=transport)

    transport2, _ = _misskey_server([[_msk_note(1), _msk_note(2)], []])
    r2 = await fetch_account(cfg_fast, maker, platform="misskey", host="misskey.io",
                             acct="artist", transport=transport2)

    assert r2.posts_new == 0
    assert r2.stopped_because == "碰到已抓過的貼文（增量）"
    assert r2.pages == 1, "碰到已知貼文就該停，不該繼續翻頁"


async def test_full_keeps_going_past_known_posts(cfg_fast, maker):
    transport, _ = _misskey_server([[_msk_note(1)], []])
    await fetch_account(cfg_fast, maker, platform="misskey", host="misskey.io",
                        acct="artist", transport=transport)

    transport2, _ = _misskey_server([[_msk_note(1)], [_msk_note(9)], []])
    r = await fetch_account(cfg_fast, maker, platform="misskey", host="misskey.io",
                            acct="artist", full=True, transport=transport2)
    assert r.posts_new == 1, "full 應該翻過已知的那頁，抓到後面的 note9"
    assert r.pages == 3


async def test_page_limit_is_reported_not_silently_hit(cfg_fast, maker):
    """撞到上限要說出來，否則使用者會以為抓完了。"""
    transport, _ = _misskey_server([[_msk_note(i)] for i in range(1, 20)])
    r = await fetch_account(cfg_fast, maker, platform="misskey", host="misskey.io",
                            acct="artist", transport=transport)
    assert r.pages == cfg_fast.fetch_max_pages
    assert "頁數上限" in r.stopped_because


async def test_same_username_on_two_instances_stays_separate(cfg_fast, maker):
    """不同 instance 的 user id 會撞 —— 撞到就是把兩個人當成同一個。"""
    t1, _ = _misskey_server([[_msk_note(1)], []])
    await fetch_account(cfg_fast, maker, platform="misskey", host="misskey.io",
                        acct="artist", transport=t1)
    t2, _ = _misskey_server([[_msk_note(1)], []])
    await fetch_account(cfg_fast, maker, platform="misskey", host="misskey.design",
                        acct="artist", transport=t2)

    with maker() as s:
        accounts = s.scalars(select(Account)).all()
        posts = s.scalars(select(Post)).all()
    assert {a.instance_host for a in accounts} == {"misskey.io", "misskey.design"}
    assert len(accounts) == 2
    assert len(posts) == 2, "同 id 不同站的貼文被當成同一則了"


async def test_auth_required_raises_instead_of_returning_empty(cfg_fast, maker):
    """403 不可以當成「沒有內容」—— 那是靜默漏抓。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "This action is not allowed"})

    with pytest.raises(MastodonAuthRequired):
        await fetch_account(cfg_fast, maker, platform="mastodon",
                            host="baraag.net", acct="artist",
                            transport=httpx.MockTransport(handler))


async def test_token_is_sent_when_configured(cfg_fast, maker):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        if request.url.path.endswith("/lookup"):
            return httpx.Response(200, json={"id": "a1", "acct": "artist"})
        return httpx.Response(200, json=[])

    cfg = dataclasses.replace(cfg_fast, instance_tokens={"baraag.net": "SECRET"})
    await fetch_account(cfg, maker, platform="mastodon", host="baraag.net",
                        acct="artist", transport=httpx.MockTransport(handler))
    assert seen["auth"] == "Bearer SECRET"


async def test_x_cannot_be_fetched_by_backend(cfg_fast, maker):
    """X 的資料來源是 extension。走這裡要明確報錯，不是回空的。"""
    with pytest.raises(ValueError, match="extension"):
        await fetch_account(cfg_fast, maker, platform="x",
                            host="x.com", acct="artist")


async def test_fetched_media_lands_in_the_download_queue(cfg_fast, maker):
    transport, _ = _misskey_server([[_msk_note(1)], []])
    await fetch_account(cfg_fast, maker, platform="misskey", host="misskey.io",
                        acct="artist", transport=transport)
    with maker() as s:
        media = s.scalars(select(Media)).all()
    assert [m.status for m in media] == ["pending"]
    assert media[0].platform_media_key == "f1"


# ── 端點 ──────────────────────────────────────────────

def test_fetch_endpoint_rejects_x(cfg):
    transport, _ = _misskey_server([[]])
    app = create_app(cfg, transport=transport)
    with TestClient(app) as client:
        r = client.post("/api/fetch",
                        json={"platform": "x", "host": "x.com",
                              "acct": "a", "wait": True})
    assert r.status_code == 400
    assert r.json()["code"] == "fetch.no_fetcher"
    # X 只能由 extension 採集 —— 這一句是使用者唯一會看到的解釋。
    assert "extension" in r.json()["detail"]


def test_fetch_endpoint_runs(cfg, tmp_path):
    transport, _ = _misskey_server([[_msk_note(1), _msk_note(2)], []])
    app = create_app(dataclasses.replace(cfg, fetch_delay_seconds=0.0),
                     transport=transport)
    with TestClient(app) as client:
        r = client.post("/api/fetch",
                        json={"platform": "misskey", "host": "misskey.io",
                              "acct": "@artist", "wait": True})
        assert r.status_code == 200
        assert r.json()["result"]["posts_new"] == 2
        assert client.get("/api/queue/status").json()["pending"] == 2
