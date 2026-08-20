"""帳號「忽略」旗標與批次編輯。

⭐ 核心性質有三個，各對應一組測試：

1. **`is_ignored` 與 `is_tracked` 是兩件事。** 效果相似（都被一鍵更新跳過），
   但來源不同 —— 後者也會被自動退訂機制寫。合併之後帳號頁就講不出
   「這是你標的」還是「系統放棄的」，而那兩者的下一步不一樣。
2. **忽略只影響 `plan_refresh()`。** 貼網址批次抓、單一 fetch 都是使用者
   明確指名了那個帳號，是覆寫，不受影響。
3. **批次一次最多 900 個 id。** SQLite 繫結變數上限是 999，超過不是慢，
   是 `OperationalError` —— 而使用者那時已經按過一個不可逆的確認鈕了。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Account
from snsmediadl.services.fetch_queue import plan_refresh


@pytest.fixture()
def client(cfg, session):
    """與 `tests/test_api.py` 同一套：client 與測試共用同一個 session，
    所以寫入之後直接讀那個 session 就看得到。"""
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def reread(session, account_id: int) -> Account:
    """讓 identity map 失效再讀 —— 端點是透過同一個 session 改的，
    但 ORM 物件可能還握著改之前的值。"""
    session.expire_all()
    return session.get(Account, account_id)


def mk(session, **kw) -> Account:
    """建一個 misskey 帳號（backend 抓得動的平台，才驗得到「可抓」）。"""
    defaults = dict(
        platform="misskey", instance_host="misskey.io",
        platform_user_id=f"u{kw.pop('n', 1)}", screen_name="someone",
    )
    defaults.update(kw)
    a = Account(**defaults)
    session.add(a)
    session.commit()
    return a


# ───────────────────────────────────── 一鍵更新的排除


def test_ignored_account_is_not_a_refresh_target(session, cfg):
    mk(session, n=1)
    mk(session, n=2, is_ignored=True)

    plan = plan_refresh(session, cfg)

    assert len(plan.targets) == 1
    assert len(plan.skipped["ignored"]) == 1


def test_ignored_is_reported_separately_from_untracked(session, cfg):
    """⚠️ 合成一個「不可抓 N 個」就等於不回答使用者的問題。

    他要知道的是「這是我自己標的（我改得回來）」還是「系統退訂的
    （我該去查是不是改名了）」。
    """
    mk(session, n=1, is_ignored=True)
    mk(session, n=2, is_tracked=False)

    plan = plan_refresh(session, cfg)

    assert len(plan.skipped["ignored"]) == 1
    assert len(plan.skipped["untracked"]) == 1


def test_ignored_wins_when_both_flags_are_off(session, cfg):
    """既被忽略又沒在追蹤時，講「你標記為忽略」——
    那是使用者剛做的事，而且是他自己改得回來的那一個。"""
    mk(session, n=1, is_ignored=True, is_tracked=False)

    plan = plan_refresh(session, cfg)

    assert plan.skipped.get("ignored")
    assert "untracked" not in plan.skipped


def test_ignoring_does_not_touch_is_tracked(session, client):
    """設為忽略時**不要**順手關掉 is_tracked。

    綁在一起的話，「這是誰做的」就再也分不出來了。
    """
    a = mk(session, n=1)
    r = client.patch(f"/api/accounts/{a.id}/prefs", json={"is_ignored": True})

    assert r.status_code == 200
    assert r.json()["is_ignored"] is True
    assert r.json()["is_tracked"] is True


# ───────────────────────────────────── 忽略的邊界


def test_ignore_does_not_block_an_explicitly_named_account(session, client):
    """貼網址批次抓 = 使用者**明確指名**了那個帳號，那是覆寫。

    忽略只管一鍵更新的目標清單。這條界線寫在 models.py 的註解裡，
    這支測試是它的守衛。
    """
    mk(session, n=1, screen_name="someone", is_ignored=True)

    r = client.post("/api/fetch/parse",
                    json={"text": "https://misskey.io/@someone"})

    assert r.status_code == 200
    line = r.json()["lines"][0]
    assert line["error"] is None, "被忽略的帳號仍然解析得出來"


# ───────────────────────────────────── 篩選


def test_filter_only_ignored(session, client):
    mk(session, n=1, is_ignored=True)
    mk(session, n=2)

    only = client.get("/api/accounts?ignored=true").json()
    rest = client.get("/api/accounts?ignored=false").json()
    both = client.get("/api/accounts").json()

    assert len(only) == 1 and only[0]["is_ignored"] is True
    assert len(rest) == 1 and rest[0]["is_ignored"] is False
    # ⚠️ 不給這個參數 = 兩者都回。`ignored=false` 是一個**真的條件**，
    # 不是「沒篩選」—— 用 `if ignored:` 而不是 `is not None` 就會搞錯這件事。
    assert len(both) == 2


def test_ids_endpoint_matches_the_list_endpoint(session, client):
    """「選取全部符合篩選的 N 個」拿到的 id 必須與畫面上那批一致。

    兩邊各寫一份 WHERE 就會漂移，而症狀是批次改到使用者沒看到的帳號 ——
    那個動作不可逆。
    """
    mk(session, n=1, is_ignored=True)
    mk(session, n=2, is_ignored=True)
    mk(session, n=3)

    listed = client.get("/api/accounts?ignored=true").json()
    ids = client.get("/api/accounts/ids?ignored=true").json()

    assert ids["total"] == len(listed) == 2
    assert ids["ids"] == sorted(a["id"] for a in listed)


# ───────────────────────────────────── 批次


def test_bulk_sets_the_flag(session, client):
    a = mk(session, n=1)
    b = mk(session, n=2)

    r = client.post("/api/accounts/bulk-prefs",
                    json={"ids": [a.id, b.id], "is_ignored": True})

    assert r.status_code == 200
    assert r.json()["updated"] == 2
    assert r.json()["missing"] == []


def test_bulk_refuses_more_than_the_sqlite_limit(client):
    """⚠️ 超過就 422，**不是靜默切**。

    一次 `IN (...)` 塞 901 個變數在 SQLite 上是 OperationalError，
    而使用者看到的會是「批次失敗」四個字 —— 那時他已經按過確認了，
    還不知道有沒有改到一半。分批是呼叫端的責任（只有它能顯示進度）。
    """
    r = client.post("/api/accounts/bulk-prefs",
                    json={"ids": list(range(1, 902)), "is_ignored": True})

    assert r.status_code == 422
    assert r.json()["code"] == "bulk.too_many_ids"
    # 上限本身仍然要出現在訊息裡 —— 呼叫端得知道該切成多大一批。
    assert "900" in r.json()["detail"]


def test_bulk_reports_missing_ids(session, client):
    """選了 3 個只改到 2 個時，那 1 個去哪了必須講得出來。"""
    a = mk(session, n=1)

    r = client.post("/api/accounts/bulk-prefs",
                    json={"ids": [a.id, 99998, 99999], "is_ignored": True})

    assert r.json()["updated"] == 1
    assert r.json()["missing"] == [99998, 99999]


def test_bulk_requires_at_least_one_field(session, client):
    """沒指定要改什麼卻送出是呼叫端的 bug。

    靜默回 `updated=0` 會讓那個 bug 潛伏 —— 畫面顯示「改好 4,653 個」
    而一筆都沒動。
    """
    a = mk(session, n=1)
    r = client.post("/api/accounts/bulk-prefs", json={"ids": [a.id]})

    assert r.status_code == 422


def test_bulk_clear_is_not_the_same_as_omitting(session, client):
    """`null` = 不動這個欄位；`__clear__` = 設成 NULL。**兩者行為不同。**

    用同一個值表示兩種意思，就會做出「想清空卻清不掉」或
    「不想動卻被清掉」的 bug。
    """
    a = mk(session, n=1, default_rating="sfw", stars=3)

    # 只改 is_ignored，兩個可清除的欄位都不帶 → 不動
    client.post("/api/accounts/bulk-prefs",
                json={"ids": [a.id], "is_ignored": True})
    row = reread(session, a.id)
    assert row.default_rating == "sfw"
    assert row.stars == 3

    # 明確送哨符 → 真的清成 NULL
    client.post("/api/accounts/bulk-prefs",
                json={"ids": [a.id], "default_rating": "__clear__",
                      "stars": "__clear__"})
    row = reread(session, a.id)
    assert row.default_rating is None
    assert row.stars is None


def test_bulk_validates_values_before_the_db_check_does(session, client):
    """值域在 API 層擋。讓 DB 的 CHECK 擋下來的話，使用者看到的是
    500 + SQLite 的英文約束名 —— 而他剛按的是一個改幾千筆的確認鈕。"""
    a = mk(session, n=1)

    assert client.post("/api/accounts/bulk-prefs",
                       json={"ids": [a.id], "stars": 9}).status_code == 422
    assert client.post("/api/accounts/bulk-prefs",
                       json={"ids": [a.id], "default_rating": "banana"}
                       ).status_code == 422


def test_bulk_retrack_also_resets_the_streak(session, client):
    """恢復追蹤要連 not_found_streak 歸零，理由同單筆那支：
    不歸零的話下一次找不到就達標，按鈕等於沒按。"""
    a = mk(session, n=1, is_tracked=False, not_found_streak=2)

    client.post("/api/accounts/bulk-prefs",
                json={"ids": [a.id], "is_tracked": True})

    row = reread(session, a.id)
    assert row.is_tracked is True
    assert row.not_found_streak == 0
