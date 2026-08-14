"""五星評分（media / accounts）、我的最愛，以及帳號的搜尋與排序。

⚠️ 全檔的 `stars` 都是**五星評分**，與 `rating`（sfw / r18 分級）無關。
兩者正交 —— 一張圖可以既是 r18 又是五星。分級的測試在 `test_api.py`。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Account, Media, Post


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture()
def loaded(client, sample_account):
    client.post(
        "/api/ingest",
        json={"platform": "x", "screenName": "sample_account", "posts": sample_account},
    )


def media_ids(client) -> list[int]:
    return [m["id"] for m in client.get("/api/media").json()["items"]]


# ---------------------------------------------------------------- media stars

def test_set_and_clear_media_stars(client, loaded):
    mid = media_ids(client)[0]

    r = client.patch(f"/api/media/{mid}/stars", json={"stars": 4})
    assert r.status_code == 200 and r.json()["stars"] == 4
    assert client.get(f"/api/media/{mid}").json()["media"]["stars"] == 4

    # 清除是送 null，不是送 0
    r = client.patch(f"/api/media/{mid}/stars", json={"stars": None})
    assert r.status_code == 200 and r.json()["stars"] is None
    assert client.get(f"/api/media/{mid}").json()["media"]["stars"] is None


@pytest.mark.parametrize("bad", [0, 6, -1, 99])
def test_out_of_range_stars_rejected_and_nothing_written(client, loaded, bad):
    mid = media_ids(client)[0]
    client.patch(f"/api/media/{mid}/stars", json={"stars": 3})

    assert client.patch(f"/api/media/{mid}/stars", json={"stars": bad}).status_code == 422
    # 被拒之後原本的值必須完好，不能被寫成一半
    assert client.get(f"/api/media/{mid}").json()["media"]["stars"] == 3


def test_media_stars_404(client):
    assert client.patch("/api/media/9999/stars", json={"stars": 3}).status_code == 404


def test_bulk_stars_hits_exactly_the_selected_media(client, loaded):
    ids = media_ids(client)
    picked, rest = ids[:3], ids[3:]

    r = client.post("/api/media/bulk-stars", json={"media_ids": picked, "stars": 5})
    assert r.json() == {"updated": 3, "requested": 3}

    got = {m["id"]: m["stars"] for m in client.get("/api/media").json()["items"]}
    assert all(got[i] == 5 for i in picked)
    # 批次打星不會波及同一則貼文的其他媒體 —— stars 掛在 media 不掛 post
    assert all(got[i] is None for i in rest)


def test_bulk_stars_empty_list_is_a_noop(client, loaded):
    assert client.post("/api/media/bulk-stars", json={"media_ids": []}).json()["updated"] == 0


def test_bulk_stars_validates_before_writing(client, loaded):
    ids = media_ids(client)
    assert client.post(
        "/api/media/bulk-stars", json={"media_ids": ids, "stars": 7}
    ).status_code == 422
    assert all(m["stars"] is None for m in client.get("/api/media").json()["items"])


def test_media_min_stars_filter(client, loaded):
    ids = media_ids(client)
    client.patch(f"/api/media/{ids[0]}/stars", json={"stars": 5})
    client.patch(f"/api/media/{ids[1]}/stars", json={"stars": 2})

    assert [m["id"] for m in client.get("/api/media?min_stars=5").json()["items"]] == [ids[0]]
    got = {m["id"] for m in client.get("/api/media?min_stars=2").json()["items"]}
    assert got == {ids[0], ids[1]}
    # 未評分（NULL）不是 0 分，一律被濾掉
    assert client.get("/api/media?min_stars=1").json()["total"] == 2


def test_media_sort_by_stars_puts_unrated_last(client, loaded):
    ids = media_ids(client)
    client.patch(f"/api/media/{ids[-1]}/stars", json={"stars": 3})
    client.patch(f"/api/media/{ids[-2]}/stars", json={"stars": 5})

    order = [m["stars"] for m in client.get("/api/media?sort=stars").json()["items"]]
    assert order[:2] == [5, 3]
    # SQLite 把 NULL 當最小值，DESC 時預設會排到最前面。這條就是在守 nullslast。
    assert all(s is None for s in order[2:])


def test_media_bad_sort_is_422_not_silently_default(client, loaded):
    assert client.get("/api/media?sort=nonsense").status_code == 422


# ------------------------------------------------------------- account prefs

def test_account_prefs_roundtrip(client, loaded):
    aid = client.get("/api/accounts").json()[0]["id"]

    r = client.patch(f"/api/accounts/{aid}/prefs", json={"stars": 5, "is_favorite": True})
    assert r.json() == {"id": aid, "stars": 5, "is_favorite": True}

    a = client.get("/api/accounts").json()[0]
    assert a["stars"] == 5 and a["is_favorite"] is True


def test_account_prefs_partial_update_leaves_the_other_alone(client, loaded):
    aid = client.get("/api/accounts").json()[0]["id"]
    client.patch(f"/api/accounts/{aid}/prefs", json={"stars": 3, "is_favorite": True})

    client.patch(f"/api/accounts/{aid}/prefs", json={"is_favorite": False})
    a = client.get("/api/accounts").json()[0]
    assert a["stars"] == 3 and a["is_favorite"] is False


def test_account_stars_can_be_cleared_with_explicit_null(client, loaded):
    """送 null 是「清除」，不是「沒指定」—— 只判斷 is not None 會讓清除變成無聲 no-op。"""
    aid = client.get("/api/accounts").json()[0]["id"]
    client.patch(f"/api/accounts/{aid}/prefs", json={"stars": 4})
    assert client.patch(f"/api/accounts/{aid}/prefs", json={"stars": None}).json()["stars"] is None


def test_account_prefs_rejects_bad_stars(client, loaded):
    aid = client.get("/api/accounts").json()[0]["id"]
    assert client.patch(f"/api/accounts/{aid}/prefs", json={"stars": 0}).status_code == 422
    assert client.patch("/api/accounts/999/prefs", json={"stars": 3}).status_code == 404


def test_account_defaults_and_prefs_are_independent(client, loaded):
    """分級預設值與個人偏好是兩件事，互不影響。"""
    aid = client.get("/api/accounts").json()[0]["id"]
    client.patch(f"/api/accounts/{aid}/defaults", json={"default_rating": "r18"})
    client.patch(f"/api/accounts/{aid}/prefs", json={"stars": 5})

    a = client.get("/api/accounts").json()[0]
    assert a["default_rating"] == "r18" and a["stars"] == 5


# --------------------------------------------------- 搜尋與排序（多帳號情境）

@pytest.fixture()
def many(session) -> dict[str, int]:
    """四個帳號，刻意讓「最後發文」與「最後採集」給出不同順序。

    | 帳號     | ♥ | 星 | 最後發文 | 最後採集 |
    |----------|---|----|----------|----------|
    | alpha    | ✓ | 3  | 2020     | 2026     |  ← 老帳號，我最近才補抓
    | beta_art |   | 5  | 2026     | 2020     |  ← 最近有發文，但我很久沒抓
    | Gamma    | ✓ | —  | —        | 2023     |  ← 沒有 posted_at
    | delta    |   | 1  | 2024     | 2024     |
    """
    base = datetime(2020, 1, 1)
    spec = [
        ("alpha", "u1", True, 3, base, base + timedelta(days=2192)),
        ("beta_art", "u2", False, 5, base + timedelta(days=2192), base),
        ("Gamma", "u3", True, None, None, base + timedelta(days=1096)),
        ("delta", "u4", False, 1, base + timedelta(days=1461), base + timedelta(days=1461)),
    ]
    ids = {}
    for i, (name, uid, fav, stars, posted, ingested) in enumerate(spec, start=1):
        acct = Account(
            platform="x", platform_user_id=uid, screen_name=name,
            is_favorite=fav, stars=stars,
        )
        session.add(acct)
        session.flush()
        ids[name] = acct.id
        post = Post(
            platform="x", platform_post_id=f"p{i}", account_id=acct.id,
            posted_at=posted, ingested_at=ingested,
        )
        session.add(post)
        session.flush()
        # 媒體數刻意與其他排序鍵不一致，才驗得出 sort=media 真的用了媒體數
        for ordinal in range(i):
            session.add(Media(post_id=post.id, ordinal=ordinal, kind="photo",
                              source_url="https://example.invalid/x.jpg"))
    session.commit()
    return ids


def names(client, query: str = "") -> list[str]:
    return [a["screen_name"] for a in client.get(f"/api/accounts{query}").json()]


def test_default_order_unchanged(client, many):
    """不帶參數必須維持插入順序 —— extension 的下拉選單與既有測試靠這個。"""
    assert names(client) == ["alpha", "beta_art", "Gamma", "delta"]


def test_search_is_case_insensitive_substring(client, many):
    assert names(client, "?q=amm") == ["Gamma"]
    assert names(client, "?q=GAMMA") == ["Gamma"]
    assert names(client, "?q=a") == ["alpha", "beta_art", "Gamma", "delta"]


def test_search_matches_platform_user_id(client, many):
    assert names(client, "?q=u3") == ["Gamma"]


def test_search_escapes_like_wildcards(client, many):
    """`_` 與 `%` 在 LIKE 裡是萬用字元，必須當成一般字元比對。

    不跳脫的話 `G_mma` 會撈到 `Gamma`（`_` 吃掉 `a`），而使用者只會覺得
    「搜尋怪怪的」—— 帳號名稱含底線是常態，這不是理論上的潔癖。
    """
    assert names(client, "?q=G_mma") == []          # 跳脫後是字面的底線，比不到
    assert names(client, "?q=beta_art") == ["beta_art"]   # 字面底線比得到
    assert names(client, "?q=%") == []              # `%` 不該變成「全部」


def test_favorite_filter(client, many):
    assert set(names(client, "?favorite=true")) == {"alpha", "Gamma"}


def test_min_stars_filter_excludes_unrated(client, many):
    assert names(client, "?min_stars=3") == ["alpha", "beta_art"]
    # Gamma 是未評分，不是 0 分 —— 不該出現在任何 min_stars 結果裡
    assert "Gamma" not in names(client, "?min_stars=1")


def test_sort_favorite_then_stars(client, many):
    # 我的最愛在前（alpha 3★ > Gamma 未評分），其餘依星等（beta 5★ > delta 1★）
    assert names(client, "?sort=favorite") == ["alpha", "Gamma", "beta_art", "delta"]


def test_sort_stars_desc_puts_unrated_last(client, many):
    assert names(client, "?sort=stars") == ["beta_art", "alpha", "delta", "Gamma"]


def test_sort_stars_asc_still_puts_unrated_last(client, many):
    """未評分永遠在最後，不論方向 —— 它不是「最小值」，是「沒有值」。"""
    assert names(client, "?sort=stars&order=asc") == ["delta", "alpha", "beta_art", "Gamma"]


def test_sort_name_is_case_insensitive(client, many):
    # 大小寫敏感排序會把 Gamma 排到所有小寫前面
    assert names(client, "?sort=name") == ["alpha", "beta_art", "delta", "Gamma"]


def test_last_post_and_last_ingest_are_genuinely_different_keys(client, many):
    """「最後更新」有兩種意思，而它們給出不同答案 —— 所以兩個都要有。"""
    by_post = names(client, "?sort=last_post")
    by_ingest = names(client, "?sort=last_ingest")
    assert by_post == ["beta_art", "delta", "alpha", "Gamma"]
    assert by_ingest == ["alpha", "delta", "Gamma", "beta_art"]
    assert by_post != by_ingest


def test_sort_last_post_puts_null_posted_at_last(client, many):
    assert names(client, "?sort=last_post")[-1] == "Gamma"


def test_sort_by_media_and_post_counts(client, many):
    assert names(client, "?sort=media") == ["delta", "Gamma", "beta_art", "alpha"]
    counts = {a["screen_name"]: a["media_count"] for a in client.get("/api/accounts").json()}
    assert counts == {"alpha": 1, "beta_art": 2, "Gamma": 3, "delta": 4}


def test_aggregates_present_for_account_with_no_posts(client, session):
    session.add(Account(platform="x", platform_user_id="lonely", screen_name="lonely"))
    session.commit()
    a = client.get("/api/accounts").json()[0]
    # LEFT JOIN 會給 NULL；回 0 而不是 null，前端才不用自己兜
    assert a["post_count"] == 0 and a["media_count"] == 0
    assert a["last_post_at"] is None and a["last_ingest_at"] is None


def test_bad_sort_and_order_are_422(client, many):
    assert client.get("/api/accounts?sort=nonsense").status_code == 422
    assert client.get("/api/accounts?sort=stars&order=sideways").status_code == 422


def test_filters_combine(client, many):
    assert names(client, "?favorite=true&sort=stars") == ["alpha", "Gamma"]
    assert names(client, "?q=a&min_stars=5") == ["beta_art"]


# ------------------------------------------------------------- 前後端契約

@pytest.mark.parametrize("url", [
    # web/app.js::loadAccountOptions —— 媒體頁的帳號下拉，刻意不帶篩選
    "/api/accounts?sort=name",
    # web/app.js::accountQuery 的各種組合
    "/api/accounts?sort=favorite",
    "/api/accounts?sort=last_ingest&q=art&favorite=true&min_stars=3",
    "/api/accounts?sort=media&q=&min_stars=1",
    # web/app.js::mediaQuery
    "/api/media?limit=60&offset=0&exclude_rating=r18&sort=newest",
    "/api/media?limit=60&offset=0&sort=stars&min_stars=4&kind=photo",
])
def test_gui_query_strings_are_accepted(client, many, url):
    """GUI 實際組出來的 URL 必須是 200。

    前端傳了後端不認得的參數時，FastAPI 預設是**默默忽略** —— 畫面看起來
    正常，只是篩選沒有生效。這條測試把 app.js 裡組 query 的那幾個函式
    釘在後端簽名上。
    """
    assert client.get(url).status_code == 200
