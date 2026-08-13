"""批次抓取與一鍵更新的端點。

`/api/fetch/parse` 必須是純讀 —— 打錯字不該變成一筆垃圾帳號記錄。
一鍵更新跳過的帳號必須逐類講出來 —— 只回數字的話，使用者會以為
X 的帳號也更新過了。
"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Account
from snsmediadl.services import fetch_queue as fq
from snsmediadl.services.fetch import FetchResult


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def no_network(monkeypatch):
    """佇列真的會去跑 —— 把抓取本身換掉，記下它收到什麼。"""
    seen: list[dict] = []

    async def fake(cfg, maker, **kw):
        seen.append(kw)
        return FetchResult(account=kw["acct"])

    monkeypatch.setattr(fq, "fetch_account", fake)
    return seen


def add_account(session, **kw) -> Account:
    defaults = dict(
        platform="misskey", instance_host="misskey.io",
        platform_user_id="u1", screen_name="artist", is_tracked=True,
    )
    acc = Account(**{**defaults, **kw})
    session.add(acc)
    session.commit()
    return acc


# ── parse ────────────────────────────────────────────────


def test_parse_classifies_every_line_and_writes_nothing(client, session):
    text = "\n".join([
        "https://misskey.io/@a",
        "https://x.com/b",
        "https://misskey.io/@a",        # 批內重複
        "# 註解",
        "https://baraag.net/@c/media",
    ])
    lines = client.post("/api/fetch/parse", json={"text": text}).json()["lines"]

    assert len(lines) == 4
    assert lines[0]["target"]["platform"] == "misskey"
    assert "extension" in lines[1]["error"]
    assert lines[2]["duplicate"] is True
    assert lines[3]["target"]["host"] == "baraag.net"

    # ⚠️ 預覽不可以留下任何東西
    assert session.scalars(select(Account)).all() == []


def test_parse_marks_accounts_already_in_db(client, session):
    add_account(session, screen_name="Artist")
    lines = client.post(
        "/api/fetch/parse", json={"text": "https://misskey.io/@artist"}
    ).json()["lines"]
    assert lines[0]["in_db"] is True          # 帳號名比對不分大小寫
    assert lines[0]["account_id"] is not None


def test_parse_does_not_confuse_two_instances(client, session):
    """同一個 handle 在兩個站上是不同的人。"""
    add_account(session, screen_name="artist", instance_host="misskey.io")
    lines = client.post(
        "/api/fetch/parse", json={"text": "https://baraag.net/@artist"}
    ).json()["lines"]
    assert lines[0]["in_db"] is False


# ── batch ────────────────────────────────────────────────


def test_batch_queues_valid_lines_and_reports_the_rest(client, no_network):
    text = "\n".join([
        "https://misskey.io/@a",
        "https://baraag.net/@b",
        "https://x.com/c",
        "https://misskey.io/@a",
    ])
    body = client.post("/api/fetch/batch", json={"text": text}).json()
    assert body["queued"] == 2
    assert len(body["rejected"]) == 1
    assert "extension" in body["rejected"][0]["error"]


def test_batch_reparses_on_the_server(client, no_network):
    """不信任前端送回來的解析結果 —— 端點自己重新解析。"""
    body = client.post("/api/fetch/batch", json={"text": "https://x.com/a"}).json()
    assert body["queued"] == 0
    assert body["rejected"]


def test_queue_status_is_readable(client, no_network):
    client.post("/api/fetch/batch", json={"text": "https://misskey.io/@a"})
    status = client.get("/api/fetch/queue").json()
    assert status["volatile"] is True
    assert "counts" in status


# ── refresh-all ──────────────────────────────────────────


def test_refresh_all_skips_are_itemised(client, session, no_network):
    """⚠️ 跳過的理由要逐類回報。

    只回一個數字的話，使用者會以為 X 的帳號也更新過了。
    """
    add_account(session, platform="misskey", platform_user_id="u1", screen_name="msk")
    add_account(session, platform="mastodon", instance_host="baraag.net",
                platform_user_id="m1", screen_name="mst")
    add_account(session, platform="x", instance_host="", platform_user_id="x1",
                screen_name="xuser")
    add_account(session, platform="misskey", platform_user_id="u2",
                screen_name="paused", is_tracked=False)
    add_account(session, platform="pixiv", instance_host="", platform_user_id="123",
                screen_name="pxv")

    body = client.post("/api/fetch/refresh-all", json={}).json()

    assert body["queued"] == 2                       # misskey + mastodon
    counts = body["skipped_counts"]
    assert counts["cannot_fetch"] == 1               # X
    assert counts["untracked"] == 1
    assert counts["pixiv_excluded"] == 1
    # 名字也要在，讓使用者看得出是哪幾個
    assert any("xuser" in s for s in body["skipped"]["cannot_fetch"])


def test_refresh_all_resolves_by_user_id_not_screen_name(client, session, no_network):
    """帳號改名是常態 —— 拿舊名字去查會 404，那個帳號從此再也更新不到。"""
    add_account(session, platform_user_id="u42", screen_name="old_name")
    client.post("/api/fetch/refresh-all", json={})

    # 佇列是背景跑的，等它處理完
    for _ in range(200):
        if no_network:
            break
        client.get("/api/fetch/queue")
    assert no_network, "佇列沒有跑到 fetch_account"
    assert no_network[0]["user_id"] == "u42"


def test_pixiv_without_credentials_is_reported_not_silently_skipped(
    client, cfg, session, no_network
):
    add_account(session, platform="pixiv", instance_host="", platform_user_id="123",
                screen_name="pxv")
    body = client.post(
        "/api/fetch/refresh-all", json={"include_pixiv": True}
    ).json()
    assert body["queued"] == 0
    assert body["skipped_counts"]["no_credentials"] == 1


def test_pixiv_runs_when_credentials_are_set(session, cfg, no_network):
    cfg2 = dataclasses.replace(cfg, platform_credentials={"pixiv": "secret"})
    app = create_app(cfg2)
    app.dependency_overrides[get_session] = lambda: session
    add_account(session, platform="pixiv", instance_host="", platform_user_id="123",
                screen_name="pxv")
    with TestClient(app) as c:
        body = c.post("/api/fetch/refresh-all", json={"include_pixiv": True}).json()
    assert body["queued"] == 1
