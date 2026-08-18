"""API 端點。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def posts_of(client, query: str = "") -> list[dict]:
    """`/api/posts` 回 {items, total, ...}，測試只關心 items。"""
    return client.get(f"/api/posts{query}").json()["items"]


def media_of(client, query: str = "") -> list[dict]:
    return client.get(f"/api/media{query}").json()["items"]


@pytest.fixture()
def loaded(client, sample_account):
    r = client.post("/api/ingest",
                    json={"platform": "x", "screenName": "sample_account",
                          "posts": sample_account})
    assert r.status_code == 200
    return r.json()


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_ingest_returns_stats(loaded):
    assert loaded["posts_new"] == 4
    assert loaded["media_new"] == 6


def test_ingest_is_idempotent(client, loaded, sample_account):
    again = client.post("/api/ingest",
                        json={"platform": "x", "posts": sample_account}).json()
    assert again["posts_new"] == 0
    assert again["media_new"] == 0
    assert again["posts_skipped"] == 4


def test_queue_status(client, loaded):
    body = client.get("/api/queue/status").json()
    assert body["pending"] == 6
    assert body["downloading"] == 0
    assert body["failed"] == 0
    assert body["active"] == 6


def test_queue_status_does_not_pretend_to_know_done(client, loaded):
    """`done` 回 None（沒算），**不可回 0**。

    0 是謊話 —— 正式庫有 224 萬筆 done。舊寫法為了數出那個數字要 412 ms，
    每 5 秒繳一次，而它回答不了任何決策。
    """
    body = client.get("/api/queue/status").json()
    assert body["done"] is None
    assert body["total"] is None
    assert body["done_exact"] is False


def test_list_posts_and_media(client, loaded):
    assert len(posts_of(client)) == 4
    assert len(media_of(client)) == 6


def test_pagination_reports_has_more(client, loaded):
    """前端要能知道還有沒有下一頁 —— 但**不靠總數**。

    總數在正式庫上要 1.3 秒（見 `list_media` 的說明），所以清單改回
    `has_more`：多撈一筆就知道，不必數完整張表。
    """
    body = client.get("/api/media?limit=2").json()
    assert body["has_more"] is True
    assert len(body["items"]) == 2
    assert body["limit"] == 2 and body["offset"] == 0
    # 總數不再由這支回 —— 誤用舊欄位要壞得明顯，不要靜默變成 None
    assert "total" not in body

    page2 = client.get("/api/media?limit=2&offset=2").json()
    assert {m["id"] for m in page2["items"]} & {m["id"] for m in body["items"]} == set()

    last = client.get("/api/media?limit=2&offset=4").json()
    assert last["has_more"] is False


def test_media_count_is_a_separate_endpoint(client, loaded):
    assert client.get("/api/media/count").json()["total"] == 6
    # 篩選參數與清單完全相同，否則兩邊會對不上
    assert client.get("/api/media/count?kind=photo").json()["total"] == \
        len(client.get("/api/media?kind=photo&limit=500").json()["items"])


def test_media_count_route_is_not_swallowed_by_media_id(client, loaded):
    """`/api/media/count` 必須宣告在 `/api/media/{media_id}` 之前。

    反過來的話 "count" 會被當成 media_id 去 parse int，回 422 —— 而且不會
    fallthrough。這條測試就是釘住宣告順序。
    """
    assert client.get("/api/media/count").status_code == 200


def test_media_keyset_pagination_walks_without_gaps(client, loaded):
    """keyset 翻頁：不重複、不漏、能走到底。"""
    all_ids = [m["id"] for m in client.get("/api/media?limit=500").json()["items"]]

    seen, cursor = [], None
    for _ in range(10):
        url = "/api/media?limit=2" + (f"&before_id={cursor}" if cursor else "")
        body = client.get(url).json()
        seen += [m["id"] for m in body["items"]]
        if not body["has_more"]:
            break
        cursor = body["next_before_id"]

    assert seen == all_ids
    assert len(seen) == len(set(seen))


def test_media_keyset_rejects_conflicting_or_unsupported_cursors(client, loaded):
    assert client.get("/api/media?before_id=1&after_id=2").status_code == 422
    # sort=stars 的排序鍵是 (stars, id) 複合又含 NULL —— 不默默改用 offset，
    # 那會讓呼叫端以為自己在做 keyset，翻頁時靜默跳筆。
    assert client.get("/api/media?sort=stars&before_id=5").status_code == 422


def test_media_detail_returns_media_post_and_account(client, loaded):
    media_id = media_of(client)[0]["id"]
    d = client.get(f"/api/media/{media_id}").json()
    assert d["media"]["id"] == media_id
    assert d["post"]["id"] == d["media"]["post_id"]
    assert d["account"]["screen_name"] == "sample_account"


def test_accounts_carry_profile_url(client, loaded):
    """帳號清單要帶「連回平台」的網址 —— 前端不得自行拼接。"""
    a = client.get("/api/accounts").json()[0]
    assert a["profile_url"] == "https://x.com/sample_account"
    assert a["link_problem"] is None
    assert a["platform_label"] == "X"
    # 既有欄位一個都不能少，否則現有前端會靜默壞掉
    for key in ("id", "platform", "screen_name", "platform_user_id", "is_tracked"):
        assert key in a


def test_media_detail_carries_post_url(client, loaded):
    media_id = media_of(client)[0]["id"]
    d = client.get(f"/api/media/{media_id}").json()
    post_id = d["post"]["platform_post_id"]
    assert d["post"]["post_url"] == f"https://x.com/sample_account/status/{post_id}"
    assert d["post"]["link_problem"] is None


def test_media_detail_works_beyond_list_page_size(client, session):
    """詳情面板不可依賴清單分頁 —— 早期版本抓 500 筆再從裡面找，
    媒體一多就必然找不到。"""
    payload = [{
        "postId": f"p{i}", "userId": "u1", "createdAt": None,
        "media": [{"kind": "photo", "url": f"https://x/{i}.jpg",
                   "orig": f"https://x/{i}.jpg?name=orig"}],
    } for i in range(600)]
    client.post("/api/ingest", json={"platform": "x", "screenName": "a",
                                     "posts": payload})

    total = client.get("/api/media/count").json()["total"]
    assert total == 600

    # 第一筆（最舊的，排序在 500 筆之外）仍然拿得到
    d = client.get("/api/media/1").json()
    assert d["media"]["id"] == 1
    assert d["post"]["platform_post_id"] == "p0"


def test_media_detail_404_for_unknown(client):
    assert client.get("/api/media/999999").status_code == 404


def test_filter_media_by_kind(client, loaded):
    kinds = {m["kind"] for m in media_of(client, "?kind=video")}
    assert kinds == {"video"}


def test_known_endpoint(client, loaded):
    ids = "1000000000000000003,doesnotexist"
    body = client.get(f"/api/known?platform=x&post_ids={ids}").json()
    assert body["known"] == ["1000000000000000003"]


def test_known_with_empty_input(client, loaded):
    assert client.get("/api/known?platform=x&post_ids=").json() == {"known": []}


def test_retry_resets_to_pending(client, loaded):
    media_id = media_of(client)[0]["id"]
    r = client.post(f"/api/media/{media_id}/retry").json()
    assert r["status"] == "pending"


def test_retry_missing_media_404(client):
    assert client.post("/api/media/9999/retry").status_code == 404


# --- 分級 ---

def test_patch_post_tags_marks_manual(client, loaded):
    post_id = posts_of(client)[0]["id"]
    r = client.patch(f"/api/posts/{post_id}/tags",
                     json={"rating": "r18", "content_type": "ai"}).json()
    assert (r["rating"], r["content_type"], r["rating_source"]) == ("r18", "ai", "manual")


def test_patch_can_clear_a_tag(client, loaded):
    """把下拉選回「未標」要真的清掉，不能被當成「沒帶這個欄位」忽略。"""
    post_id = posts_of(client)[0]["id"]
    client.patch(f"/api/posts/{post_id}/tags", json={"rating": "r18"})

    r = client.patch(f"/api/posts/{post_id}/tags", json={"rating": None}).json()
    assert r["rating"] is None


def test_patch_only_touches_provided_fields(client, loaded):
    """自動儲存一次只送一個欄位，不可把另一個洗掉。"""
    post_id = posts_of(client)[0]["id"]
    client.patch(f"/api/posts/{post_id}/tags",
                 json={"rating": "r18", "content_type": "ai"})

    r = client.patch(f"/api/posts/{post_id}/tags", json={"rating": "sfw"}).json()
    assert r["rating"] == "sfw"
    assert r["content_type"] == "ai"


def test_patch_rejects_bad_rating(client, loaded):
    post_id = posts_of(client)[0]["id"]
    r = client.patch(f"/api/posts/{post_id}/tags", json={"rating": "nope"})
    assert r.status_code == 422


def test_exclude_rating_keeps_unknown(client, loaded):
    """NULL 是「未知」不是「被排除的那一級」，不可被一起濾掉。"""
    posts = posts_of(client)
    client.patch(f"/api/posts/{posts[0]['id']}/tags", json={"rating": "r18"})

    remaining = posts_of(client, "?exclude_rating=r18")
    assert len(remaining) == 3
    assert all(p["rating"] is None for p in remaining)


def test_account_defaults_do_not_backfill(client, loaded):
    account_id = client.get("/api/accounts").json()[0]["id"]
    client.patch(f"/api/accounts/{account_id}/defaults", json={"default_rating": "r18"})
    assert all(p["rating"] is None for p in posts_of(client))


def test_retag_applies_defaults(client, loaded):
    account_id = client.get("/api/accounts").json()[0]["id"]
    client.patch(f"/api/accounts/{account_id}/defaults", json={"default_rating": "r18"})
    r = client.post(f"/api/accounts/{account_id}/retag", json={}).json()
    assert r["updated"] == 4
    assert all(p["rating"] == "r18" for p in posts_of(client))


def test_retag_preserves_manual_by_default(client, loaded):
    posts = posts_of(client)
    client.patch(f"/api/posts/{posts[0]['id']}/tags", json={"rating": "sfw"})

    account_id = client.get("/api/accounts").json()[0]["id"]
    client.patch(f"/api/accounts/{account_id}/defaults", json={"default_rating": "r18"})
    r = client.post(f"/api/accounts/{account_id}/retag", json={}).json()

    assert r["updated"] == 3
    kept = [p for p in posts_of(client) if p["id"] == posts[0]["id"]][0]
    assert kept["rating"] == "sfw"


def test_retag_can_overwrite_manual_when_asked(client, loaded):
    posts = posts_of(client)
    client.patch(f"/api/posts/{posts[0]['id']}/tags", json={"rating": "sfw"})

    account_id = client.get("/api/accounts").json()[0]["id"]
    client.patch(f"/api/accounts/{account_id}/defaults", json={"default_rating": "r18"})
    r = client.post(f"/api/accounts/{account_id}/retag",
                    json={"overwrite_manual": True}).json()

    assert r["updated"] == 4
    assert all(p["rating"] == "r18" for p in posts_of(client))


def test_retag_without_defaults_is_422(client, loaded):
    account_id = client.get("/api/accounts").json()[0]["id"]
    assert client.post(f"/api/accounts/{account_id}/retag", json={}).status_code == 422


# --- creators ---

def test_creator_can_hold_two_same_platform_accounts(client, session):
    from snsmediadl.db.models import Account

    session.add_all([
        Account(platform="x", platform_user_id="1", screen_name="artist"),
        Account(platform="x", platform_user_id="2", screen_name="artist_r18"),
    ])
    session.commit()

    creator = client.post("/api/creators", json={"display_name": "某畫師"}).json()
    accounts = client.get("/api/accounts").json()

    client.post(f"/api/accounts/{accounts[0]['id']}/link",
                json={"creator_id": creator["id"], "role": "main"})
    client.post(f"/api/accounts/{accounts[1]['id']}/link",
                json={"creator_id": creator["id"], "role": "r18_alt"})

    got = client.get(f"/api/creators/{creator['id']}").json()
    assert len(got["accounts"]) == 2
    assert {a["role"] for a in got["accounts"]} == {"main", "r18_alt"}


def test_creator_media_spans_accounts(client, session, sample_account):
    client.post("/api/ingest", json={"platform": "x", "screenName": "a",
                                     "posts": sample_account})
    creator = client.post("/api/creators", json={"display_name": "某畫師"}).json()
    account_id = client.get("/api/accounts").json()[0]["id"]
    client.post(f"/api/accounts/{account_id}/link",
                json={"creator_id": creator["id"], "role": "main"})

    media = client.get(f"/api/creators/{creator['id']}/media").json()
    assert len(media) == 6


def test_link_rejects_bad_role(client, session):
    from snsmediadl.db.models import Account
    session.add(Account(platform="x", platform_user_id="1"))
    session.commit()
    creator = client.post("/api/creators", json={"display_name": "x"}).json()
    account_id = client.get("/api/accounts").json()[0]["id"]
    r = client.post(f"/api/accounts/{account_id}/link",
                    json={"creator_id": creator["id"], "role": "boss"})
    assert r.status_code == 422


def test_unlink_account(client, session):
    from snsmediadl.db.models import Account
    session.add(Account(platform="x", platform_user_id="1"))
    session.commit()
    creator = client.post("/api/creators", json={"display_name": "x"}).json()
    account_id = client.get("/api/accounts").json()[0]["id"]
    client.post(f"/api/accounts/{account_id}/link", json={"creator_id": creator["id"]})
    client.delete(f"/api/accounts/{account_id}/link")
    assert client.get("/api/accounts").json()[0]["creator_id"] is None


def test_filter_posts_by_creator(client, session, sample_account):
    client.post("/api/ingest", json={"platform": "x", "posts": sample_account})
    creator = client.post("/api/creators", json={"display_name": "x"}).json()
    account_id = client.get("/api/accounts").json()[0]["id"]
    client.post(f"/api/accounts/{account_id}/link", json={"creator_id": creator["id"]})

    assert len(posts_of(client, f"?creator_id={creator['id']}")) == 4
    assert posts_of(client, "?creator_id=999") == []
