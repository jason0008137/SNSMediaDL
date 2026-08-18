"""排序（鍵 × 方向、NULL 分段游標）與多選篩選。

⭐ 兩個核心性質：

1. **`posted_at` 為 NULL 的媒體在升冪與降冪都排最後，而且翻得過去。**
   「時間未知」不是「很早」也不是「很晚」—— 把它排進時間軸上任何一個位置
   都是在編造資訊。而 keyset 分頁遇到 NULL 會直接斷掉（NULL 的比較結果
   是 NULL 不是 true），所以要分兩段翻。

2. **全勾 ≠ 不勾。** `rating IN ('sfw','r18')` 會濾掉 rating 為 NULL 的那些；
   不勾是全都要。刻意不做「全勾自動視為不勾」的貼心處理。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.enums import MediaStatus
from snsmediadl.db.models import Account, Media, Post


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _seed(session, rows):
    """rows = [(posted_at, kind, rating, content_type)]，依序建立。

    每筆一則貼文一個媒體 —— 這裡要驗的是排序與篩選，不是同貼文多張的行為。
    """
    acct = Account(platform="x", platform_user_id="u1", screen_name="someone")
    session.add(acct)
    session.flush()
    made = []
    for i, (posted, kind, rating, ctype) in enumerate(rows):
        post = Post(platform="x", platform_post_id=f"p{i}", account_id=acct.id,
                    posted_at=posted, rating=rating, content_type=ctype)
        session.add(post)
        session.flush()
        m = Media(post_id=post.id, ordinal=0, kind=kind,
                  source_url=f"https://example.invalid/{i}",
                  status=MediaStatus.DONE.value, posted_at=posted)
        session.add(m)
        session.flush()
        made.append(m.id)
    session.commit()
    return made


def dt(day: int) -> datetime:
    return datetime(2026, 1, day)


def ids(body) -> list[int]:
    return [m["id"] for m in body["items"]]


# ── 排序鍵與方向拆開 ──────────────────────────────────

def test_old_sort_values_still_work(client, session):
    """`newest` / `oldest` 是舊值，保留成 alias —— 書籤網址與既有呼叫端不該壞。"""
    made = _seed(session, [(dt(1), "photo", None, None)] * 3)

    assert ids(client.get("/api/media?sort=newest").json()) == made[::-1]
    assert ids(client.get("/api/media?sort=oldest").json()) == made
    # 新寫法等價
    assert ids(client.get("/api/media?sort=added&order=desc").json()) == made[::-1]
    assert ids(client.get("/api/media?sort=added&order=asc").json()) == made


def test_unknown_sort_or_order_is_422(client, session):
    """不默默改用預設 —— 那會讓打錯字看起來像「排序功能壞了」。"""
    assert client.get("/api/media?sort=banana").status_code == 422
    assert client.get("/api/media?sort=posted&order=sideways").status_code == 422


def test_sort_posted_desc_and_asc(client, session):
    made = _seed(session, [
        (dt(2), "photo", None, None),
        (dt(1), "photo", None, None),
        (dt(3), "photo", None, None),
    ])
    by_date = {made[1]: 1, made[0]: 2, made[2]: 3}

    desc = ids(client.get("/api/media?sort=posted&order=desc").json())
    assert [by_date[i] for i in desc] == [3, 2, 1]

    asc = ids(client.get("/api/media?sort=posted&order=asc").json())
    assert [by_date[i] for i in asc] == [1, 2, 3]


# ── NULL 永遠排最後（升冪降冪都是）─────────────────────

def test_nulls_are_last_in_both_directions(client, session):
    made = _seed(session, [
        (dt(2), "photo", None, None),
        (None, "photo", None, None),
        (dt(1), "photo", None, None),
        (None, "photo", None, None),
    ])
    nulls = {made[1], made[3]}

    for order in ("desc", "asc"):
        got = ids(client.get(f"/api/media?sort=posted&order={order}").json())
        assert set(got[-2:]) == nulls, f"order={order} 時 NULL 沒有排在最後：{got}"


def test_paging_through_the_null_boundary_loses_nothing(client, session):
    """⭐ 翻頁跨越「有時間 → 沒時間」的交界，不重複也不跳過。

    這是兩段式游標存在的唯一理由。單一 keyset 在這裡會直接回空頁 ——
    而症狀是「翻到一半就沒了」，看起來像資料只有那麼多。
    """
    rows = [(dt(d), "photo", None, None) for d in range(1, 6)]
    rows += [(None, "photo", None, None)] * 4
    made = _seed(session, rows)

    for order in ("desc", "asc"):
        seen: list[int] = []
        cursor = None
        for _ in range(20):          # 上限保護，不要無限迴圈
            url = f"/api/media?sort=posted&order={order}&limit=2"
            if cursor:
                url += f"&cursor={cursor}"
            body = client.get(url).json()
            assert body is not None
            seen += ids(body)
            cursor = body.get("next_cursor")
            if not body["has_more"]:
                break

        assert len(seen) == len(set(seen)), f"order={order} 有重複：{seen}"
        assert sorted(seen) == sorted(made), (
            f"order={order} 漏了 {set(made) - set(seen)}")


def test_bad_cursor_is_422_not_a_silent_restart(client, session):
    """⭐ 從頭開始的症狀是「翻到第 30 頁突然跳回第 1 頁」——
    使用者只會覺得分頁壞了，不會回報成游標格式錯誤。"""
    _seed(session, [(dt(1), "photo", None, None)])
    for bad in ("banana", "p:not-a-date|5", "p:2026-01-01", "n:abc", "x:1"):
        r = client.get(f"/api/media?sort=posted&cursor={bad}")
        assert r.status_code == 422, f"{bad!r} 應該回 422，實際 {r.status_code}"


def test_stars_sort_still_refuses_keyset(client, session):
    """默默改用 offset 會讓呼叫端以為自己在做 keyset，翻頁時靜默跳筆。"""
    _seed(session, [(dt(1), "photo", None, None)])
    assert client.get("/api/media?sort=stars&before_id=5").status_code == 422
    assert client.get("/api/media?sort=stars&cursor=5").status_code == 422


# ── 多選篩選 ──────────────────────────────────────────

def test_kind_multi_is_a_union(client, session):
    made = _seed(session, [
        (dt(1), "photo", None, None),
        (dt(2), "video", None, None),
        (dt(3), "animated_gif", None, None),
    ])

    body = client.get("/api/media?kind=video&kind=animated_gif").json()
    assert set(ids(body)) == {made[1], made[2]}

    # 逗號分隔（手打網址用）等價
    body = client.get("/api/media?kind=video,animated_gif").json()
    assert set(ids(body)) == {made[1], made[2]}


def test_fields_are_anded_values_are_ored(client, session):
    """欄位之間 AND，欄位之內 OR。"""
    made = _seed(session, [
        (dt(1), "video", "sfw", None),
        (dt(2), "video", "r18", None),
        (dt(3), "photo", "sfw", None),
    ])
    body = client.get("/api/media?kind=video&kind=photo&rating=sfw").json()
    assert set(ids(body)) == {made[0], made[2]}


def test_unknown_filter_value_is_422(client, session):
    """⭐ 靜默忽略錯字會讓使用者看到一份「篩選好像沒生效」的畫面。"""
    _seed(session, [(dt(1), "photo", None, None)])
    assert client.get("/api/media?kind=banana").status_code == 422
    assert client.get("/api/media?rating=nsfw").status_code == 422
    assert client.get("/api/media?status=uploading").status_code == 422
    assert client.get("/api/media?content_type=nonsense").status_code == 422
    # count 走同一條驗證
    assert client.get("/api/media/count?kind=banana").status_code == 422


def test_empty_value_means_no_filter(client, session):
    """`?kind=` 的意思是「這個欄位不篩選」，不是「篩選空字串」。

    前端的「清除篩選」常常就是送一個空值 —— 不吃這一種的話會變成 422。
    """
    made = _seed(session, [(dt(1), "photo", None, None), (dt(2), "video", None, None)])
    assert set(ids(client.get("/api/media?kind=").json())) == set(made)


def test_checking_everything_is_not_the_same_as_checking_nothing(client, session):
    """⭐ 全勾 ≠ 不勾。

    rating 為 NULL 的那些在「全勾」時會被濾掉 —— 正式庫有 1,072 筆。
    刻意**不做**「全勾自動視為不勾」的貼心處理：那會讓使用者永遠看不到
    未分級的資料，而且沒有任何提示。
    """
    made = _seed(session, [
        (dt(1), "photo", "sfw", None),
        (dt(2), "photo", "r18", None),
        (dt(3), "photo", None, None),      # 未分級
    ])

    everything = client.get("/api/media?rating=sfw&rating=r18").json()
    nothing = client.get("/api/media").json()

    assert set(ids(everything)) == {made[0], made[1]}
    assert set(ids(nothing)) == set(made)
    assert len(ids(everything)) < len(ids(nothing))


def test_count_matches_the_list_for_multi_values(client, session):
    """總數與清單共用同一份條件 —— 對不上的話看起來像分頁壞了。"""
    _seed(session, [
        (dt(1), "video", "sfw", None),
        (dt(2), "animated_gif", "r18", None),
        (dt(3), "photo", "sfw", None),
    ])
    q = "kind=video&kind=animated_gif"
    assert (client.get(f"/api/media/count?{q}").json()["total"]
            == len(ids(client.get(f"/api/media?{q}").json())) == 2)


def test_safe_mode_and_rating_multi_coexist_without_rewriting(client, session):
    """安全模式（exclude_rating）與 rating 多選並存時，API **不做任何合併**。

    使用者勾了 r18 又開著安全模式，就是永遠 0 筆 —— 那是真話。
    UI 負責把 r18 選項 disable 並寫出原因；API 不替使用者改寫他的條件。
    """
    _seed(session, [(dt(1), "photo", "r18", None), (dt(2), "photo", "sfw", None)])
    body = client.get("/api/media?rating=r18&exclude_rating=r18").json()
    assert ids(body) == []
