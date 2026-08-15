"""從貼文推導帳號預設值（`POST /api/accounts/derive-defaults`）。

方向與 `/accounts/{id}/retag` 相反：那支是「帳號預設值 → 貼文」，
這支是「貼文 → 帳號預設值」。舊資料匯入之後貼文都標好了，帳號層卻全是空的，
而帳號層才是瀏覽時真正在用的篩選維度。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Account, Post


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def seed(session, spec: dict[str, list[tuple[str | None, str | None]]]) -> dict[str, int]:
    """spec: {帳號名: [(rating, content_type), …]}"""
    ids = {}
    n = 0
    for name, posts in spec.items():
        acc = Account(platform="x", platform_user_id=f"u_{name}", screen_name=name)
        session.add(acc)
        session.flush()
        ids[name] = acc.id
        for rating, ctype in posts:
            n += 1
            session.add(Post(platform="x", platform_post_id=f"p{n}",
                             account_id=acc.id, rating=rating, content_type=ctype))
    session.commit()
    return ids


def accounts(client) -> dict[str, dict]:
    return {a["screen_name"]: a for a in client.get("/api/accounts").json()}


def test_unanimous_accounts_get_their_own_tags(client, session):
    seed(session, {
        "sfw_artist": [("sfw", "illust")] * 3,
        "r18_artist": [("r18", "illust")] * 3,
        "irl_person": [("sfw", "irl")] * 2,
    })
    r = client.post("/api/accounts/derive-defaults", json={}).json()
    assert r["updated"] == {"rating": 3, "content_type": 3}

    a = accounts(client)
    assert (a["sfw_artist"]["default_rating"], a["sfw_artist"]["default_content_type"]) == ("sfw", "illust")
    assert a["r18_artist"]["default_rating"] == "r18"
    assert a["irl_person"]["default_content_type"] == "irl"


def test_mixed_account_becomes_r18(client, session):
    """⭐ 只要出現過一次 r18 就標 r18，不看比例。

    default_rating 會被之後 ingest 的新貼文繼承。把「99% sfw 但偶爾畫 r18」
    的作者標成 sfw，代價是新的 r18 作品在工作安全模式下直接出現在畫面上；
    標成 r18 的代價只是被多藏起來。猜錯的方向不對稱。
    """
    seed(session, {"mostly_sfw": [("sfw", "illust")] * 99 + [("r18", "illust")]})
    r = client.post("/api/accounts/derive-defaults", json={}).json()
    assert accounts(client)["mostly_sfw"]["default_rating"] == "r18"
    assert r["mixed_accounts"]["n"] == 1
    assert "mostly_sfw" in r["mixed_accounts"]["sample"]


def test_content_type_takes_the_mode(client, session):
    """類型取眾數 —— 它沒有安全上的不對稱，所以不套「最嚴格者勝」。"""
    seed(session, {"mixed_types": [("sfw", "illust")] * 5 + [("sfw", "irl")] * 2})
    client.post("/api/accounts/derive-defaults", json={})
    assert accounts(client)["mixed_types"]["default_content_type"] == "illust"


def test_untagged_accounts_are_left_alone(client, session):
    seed(session, {"unknown": [(None, None)] * 3})
    r = client.post("/api/accounts/derive-defaults", json={}).json()
    assert r["updated"] == {"rating": 0, "content_type": 0}
    a = accounts(client)["unknown"]
    assert a["default_rating"] is None and a["default_content_type"] is None


def test_existing_values_are_not_overwritten_by_default(client, session):
    """已經設過的多半是人工設的，不該被推導結果蓋掉。"""
    ids = seed(session, {"manual": [("sfw", "illust")] * 3})
    client.patch(f"/api/accounts/{ids['manual']}/defaults",
                 json={"default_rating": "r18", "default_content_type": "ai"})

    r = client.post("/api/accounts/derive-defaults", json={}).json()
    assert r["updated"] == {"rating": 0, "content_type": 0}
    a = accounts(client)["manual"]
    assert (a["default_rating"], a["default_content_type"]) == ("r18", "ai")

    # 明講要覆蓋才會動
    r = client.post("/api/accounts/derive-defaults", json={"overwrite": True}).json()
    assert r["updated"] == {"rating": 1, "content_type": 1}
    a = accounts(client)["manual"]
    assert (a["default_rating"], a["default_content_type"]) == ("sfw", "illust")


def test_dry_run_changes_nothing(client, session):
    seed(session, {"artist": [("r18", "ai")] * 2})
    r = client.post("/api/accounts/derive-defaults", json={"dry_run": True}).json()
    assert r["dry_run"] is True
    assert r["updated"] == {"rating": 1, "content_type": 1}   # 會改幾筆仍要回報
    a = accounts(client)["artist"]
    assert a["default_rating"] is None, "dry_run 卻真的寫進去了"


def test_single_account_scope(client, session):
    ids = seed(session, {"a1": [("sfw", "illust")], "a2": [("r18", "illust")]})
    client.post("/api/accounts/derive-defaults", json={"account_id": ids["a1"]})
    a = accounts(client)
    assert a["a1"]["default_rating"] == "sfw"
    assert a["a2"]["default_rating"] is None


def test_derived_defaults_are_filterable(client, session):
    """使用者要的就是這個：帳號層能直接篩。"""
    seed(session, {"s": [("sfw", "illust")], "r": [("r18", "illust")]})
    client.post("/api/accounts/derive-defaults", json={})
    names = [a["screen_name"] for a in client.get("/api/accounts").json()
             if a["default_rating"] == "r18"]
    assert names == ["r"]
