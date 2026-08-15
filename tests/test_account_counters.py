"""`accounts` 聚合欄的維護與漂移偵測。

去正規化的風險是「某條寫入路徑忘了維護」變成靜默錯誤。這一整份測試存在的
理由就是讓那件事變成 red，而不是變成幾個月後才發現的錯數字。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Account, Media, Post
from snsmediadl.services import counters, deletion


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _account(client, account_id: int) -> dict:
    return next(a for a in client.get("/api/accounts").json() if a["id"] == account_id)


# ── ingest 路徑 ────────────────────────────────────────────

def test_ingest_updates_counters(client, sample_account):
    r = client.post("/api/ingest", json={"platform": "x",
                                         "screenName": "sample_account",
                                         "posts": sample_account}).json()
    a = _account(client, r["account_id"])
    assert a["post_count"] == 4
    assert a["media_count"] == 6
    assert a["last_post_at"] is not None


def test_ingest_twice_does_not_double_count(client, sample_account):
    """增量去重：重跑同一批，計數不可翻倍。

    這條在「加減法」的實作下極易失守（`+= posts_new` 看起來也對），
    改用重算就不可能錯 —— 但仍要有測試釘住，因為日後有人可能改回加減法。
    """
    body = {"platform": "x", "screenName": "sample_account", "posts": sample_account}
    r = client.post("/api/ingest", json=body).json()
    client.post("/api/ingest", json=body)

    a = _account(client, r["account_id"])
    assert (a["post_count"], a["media_count"]) == (4, 6)


def test_ingest_spanning_two_accounts_updates_both(client, session):
    """一批貼文可能來自多個帳號 —— 不可只更新第一個。"""
    payload = [
        {"postId": "p1", "userId": "u1", "createdAt": None,
         "media": [{"kind": "photo", "url": "https://x/1.jpg"}]},
        {"postId": "p2", "userId": "u2", "createdAt": None,
         "media": [{"kind": "photo", "url": "https://x/2.jpg"},
                   {"kind": "photo", "url": "https://x/3.jpg"}]},
    ]
    client.post("/api/ingest", json={"platform": "x", "posts": payload})

    by_uid = {a["platform_user_id"]: a for a in client.get("/api/accounts").json()}
    assert (by_uid["u1"]["post_count"], by_uid["u1"]["media_count"]) == (1, 1)
    assert (by_uid["u2"]["post_count"], by_uid["u2"]["media_count"]) == (1, 2)


# ── 刪除路徑 ──────────────────────────────────────────────

def test_delete_post_shrinks_counters(client, session, sample_account):
    r = client.post("/api/ingest", json={"platform": "x",
                                         "posts": sample_account}).json()
    post_id = client.get("/api/posts").json()["items"][0]["id"]
    n_media = len(session.get(Post, post_id).media)

    deletion.delete_post(session, post_id)

    a = _account(client, r["account_id"])
    assert a["post_count"] == 3
    assert a["media_count"] == 6 - n_media


def test_delete_media_shrinks_media_count_only(client, session, sample_account):
    r = client.post("/api/ingest", json={"platform": "x",
                                         "posts": sample_account}).json()
    media_id = client.get("/api/media").json()["items"][0]["id"]

    deletion.delete_media(session, media_id)

    a = _account(client, r["account_id"])
    assert a["media_count"] == 5
    assert a["post_count"] == 4      # 貼文還在


# ── 漂移偵測 ──────────────────────────────────────────────

def test_check_is_clean_after_normal_ingest(client, session, sample_account):
    client.post("/api/ingest", json={"platform": "x", "posts": sample_account})
    assert counters.check(session) == []


def test_check_catches_drift(client, session, sample_account):
    """把快取值改錯 → `check()` 要抓到，而且要說出差在哪。

    這模擬的是「某條寫入路徑漏了 recompute」。抓不到的話，那條路徑會一直
    漏下去，數字慢慢偏掉而畫面照樣顯示得理直氣壯。
    """
    r = client.post("/api/ingest", json={"platform": "x",
                                         "posts": sample_account}).json()
    acct = session.get(Account, r["account_id"])
    acct.post_count = 999
    session.commit()

    bad = counters.check(session)
    assert len(bad) == 1
    assert bad[0]["id"] == r["account_id"]
    assert bad[0]["diffs"]["post_count"] == (999, 4)
    # 只報有問題的欄位，沒問題的不列進來
    assert "media_count" not in bad[0]["diffs"]


def test_recompute_fixes_drift(client, session, sample_account):
    r = client.post("/api/ingest", json={"platform": "x",
                                         "posts": sample_account}).json()
    acct = session.get(Account, r["account_id"])
    acct.post_count = 999
    acct.media_count = 0
    session.commit()

    counters.recompute(session, [r["account_id"]])
    session.commit()
    assert counters.check(session) == []


def test_recompute_all_when_no_ids_given(client, session, sample_account):
    client.post("/api/ingest", json={"platform": "x", "posts": sample_account})
    session.execute(Account.__table__.update().values(post_count=0, media_count=0))
    session.commit()
    assert counters.check(session) != []

    counters.recompute(session)      # 不給 id = 全部
    session.commit()
    assert counters.check(session) == []


def test_account_with_no_posts_has_zero_not_null(client, session):
    """LEFT JOIN 時代會給 NULL。欄位是 NOT NULL DEFAULT 0，前端不用自己兜。"""
    session.add(Account(platform="x", platform_user_id="lonely", screen_name="lonely"))
    session.commit()
    counters.recompute(session)
    session.commit()

    a = client.get("/api/accounts").json()[0]
    assert a["post_count"] == 0 and a["media_count"] == 0
    assert a["last_post_at"] is None and a["last_ingest_at"] is None


# ── CLI ───────────────────────────────────────────────────

def test_recount_cli_check_exits_nonzero_on_drift(client, session, sample_account,
                                                  monkeypatch, capsys, cfg):
    """`--check` 發現不一致要 **exit 1** —— 只印訊息的話，排進自動檢查也沒用。"""
    from snsmediadl import cli

    r = client.post("/api/ingest", json={"platform": "x",
                                         "posts": sample_account}).json()
    session.get(Account, r["account_id"]).post_count = 42
    session.commit()

    class _Maker:
        def __call__(self):
            return _Ctx(session)

    class _Ctx:
        def __init__(self, s):
            self.s = s

        def __enter__(self):
            return self.s

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cli, "_bootstrap", lambda: (cfg, _Maker()))

    assert cli.main(["recount-accounts"]) == 1
    out = capsys.readouterr().out
    assert "不一致" in out
    assert "42" in out                     # 要說出存的是什麼
    assert "什麼都沒改" in out              # 預設不修正

    # --fix 才寫入
    assert cli.main(["recount-accounts", "--fix"]) == 0
    assert counters.check(session) == []
