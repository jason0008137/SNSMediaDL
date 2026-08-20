"""`/api/accounts` 的 `sort` × `order`，以及前端那份預設方向表沒有漂移。

⚠️ **這一組不是為了改後端。** 後端早就收 `order` 了（`query.py` 的
`order: str | None`），只是前端從來沒傳過 —— 帳號頁的排序方向以前烘在選項
文字裡（「評分高到低」），十個鍵有七個的方向根本改不了。

前端把排序拆成「鍵 + 方向」之後，這些組合第一次真的會被送出來。所以這裡
釘住三件事：

1. **十個鍵 × 兩個方向都回 200。** 前端拆分不可以依賴任何未定義行為。
2. **不認得的值 422，不默默退回預設。** 那會讓「參數打錯」看起來像
   「排序功能壞了」。
3. **`last_fetch` 的 NULL 規則是刻意反轉的。** 升冪時「從沒檢查過」排最前，
   因為那些最該查。方向鈕一按就翻，這個行為第一次會被使用者看見 ——
   壞掉的話畫面上看起來只是「順序怪怪的」。

第四條在 `test_default_order_table_matches_backend`：前端的 `A_DEFAULT_ORDER`
與後端 `sorts` 的 `default_desc` 必須逐項對齊。對不齊的症狀是選到某個鍵時
箭頭與實際順序相反 —— 不報錯，只是騙人。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Account

ROOT = pathlib.Path(__file__).resolve().parent.parent

SORT_KEYS = [
    "favorite", "stars", "name", "last_post", "last_ingest",
    "last_fetch", "media", "posts", "created", "id",
]


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def mk(session, n: int, **kw) -> Account:
    defaults = dict(
        platform="misskey", instance_host="misskey.io",
        platform_user_id=f"u{n}", screen_name=f"acct{n:02d}",
    )
    defaults.update(kw)
    a = Account(**defaults)
    session.add(a)
    session.commit()
    return a


# ───────────────────────────────────── 1. 十個鍵 × 兩個方向


@pytest.mark.parametrize("sort", SORT_KEYS)
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_every_key_and_order_is_accepted(client, session, sort, order):
    mk(session, 1)
    mk(session, 2, stars=3, is_favorite=True)

    r = client.get("/api/accounts", params={"sort": sort, "order": order})

    assert r.status_code == 200, r.text
    assert len(r.json()) == 2


def test_order_actually_reverses(client, session):
    """兩個方向要真的不一樣。都回 200 但順序一樣的話，等於方向鈕是假的。"""
    mk(session, 1)
    mk(session, 2)
    mk(session, 3)

    asc = [a["screen_name"] for a in
           client.get("/api/accounts", params={"sort": "name", "order": "asc"}).json()]
    desc = [a["screen_name"] for a in
            client.get("/api/accounts", params={"sort": "name", "order": "desc"}).json()]

    assert asc == sorted(asc)
    assert desc == list(reversed(asc))


# ───────────────────────────────────── 2. 認不得就 422


@pytest.mark.parametrize("params", [
    {"sort": "banana"},
    {"sort": "SELECT"},
    {"order": "sideways"},
    {"sort": "name", "order": "ASC"},      # 大小寫也不放行：值域就是小寫兩個
])
def test_unknown_values_are_rejected(client, session, params):
    mk(session, 1)

    r = client.get("/api/accounts", params=params)

    assert r.status_code == 422, f"{params} 應該被拒絕，實際 {r.status_code}"


def test_default_order_is_used_when_order_omitted(client, session):
    """不給 `order` 時走該鍵的預設方向，不是硬性 desc。

    `name` 的預設是升冪 —— 這是前端 `A_DEFAULT_ORDER` 依賴的行為。
    """
    mk(session, 1)
    mk(session, 2)

    names = [a["screen_name"] for a in
             client.get("/api/accounts", params={"sort": "name"}).json()]

    assert names == sorted(names)


# ───────────────────────────────────── 3. last_fetch 的 NULL 反轉


def test_never_fetched_sorts_first_on_ascending(client, session):
    """升冪時「從沒檢查過」排**最前面** —— 那些最該查。

    ⚠️ 這與其他所有鍵的 nullslast **相反**，而且是刻意的
    （`query.py` 的 `if sort == "last_fetch"`）。
    """
    now = dt.datetime(2026, 8, 1, 12, 0, 0)
    mk(session, 1, last_fetched_at=now)
    mk(session, 2)                                   # 從沒檢查過
    mk(session, 3, last_fetched_at=now - dt.timedelta(days=30))

    rows = client.get("/api/accounts",
                      params={"sort": "last_fetch", "order": "asc"}).json()

    assert rows[0]["last_fetched_at"] is None
    # 其餘由舊到新
    assert [r["screen_name"] for r in rows[1:]] == ["acct03", "acct01"]


def test_never_fetched_sorts_last_on_descending(client, session):
    """按一下方向鈕，那一批要跑到最後面。

    這正是 `#aSortNote` 兩句話存在的理由：沒有註記的話，最上面那批整個消失
    看起來就是 bug。
    """
    now = dt.datetime(2026, 8, 1, 12, 0, 0)
    mk(session, 1, last_fetched_at=now)
    mk(session, 2)
    mk(session, 3, last_fetched_at=now - dt.timedelta(days=30))

    rows = client.get("/api/accounts",
                      params={"sort": "last_fetch", "order": "desc"}).json()

    assert rows[-1]["last_fetched_at"] is None
    assert [r["screen_name"] for r in rows[:2]] == ["acct01", "acct03"]


@pytest.mark.parametrize("sort,field", [
    ("stars", "stars"),
    ("last_post", "last_post_at"),
    ("last_ingest", "last_ingest_at"),
])
def test_other_keys_keep_nulls_last(client, session, sort, field):
    """其餘可為 NULL 的鍵一律沉底，**兩個方向都是**。

    SQLite 把 NULL 當最小值，DESC 時它會冒到最前面 —— 未評分的帳號壓在
    五星前面。`#aSortNote` 對這幾個鍵講的就是這件事。
    """
    now = dt.datetime(2026, 8, 1, 12, 0, 0)
    mk(session, 1, **{field: (3 if field == "stars" else now)})
    mk(session, 2)                                   # 該欄位是 NULL

    for order in ("asc", "desc"):
        rows = client.get("/api/accounts",
                          params={"sort": sort, "order": order}).json()
        assert rows[-1]["screen_name"] == "acct02", f"{sort}/{order} 的 NULL 沒有沉底"


# ───────────────────────────────────── 4. 前後端的預設方向表不可漂移


def _js_default_order() -> dict[str, str]:
    """抽 `views/accounts.js` 的 `A_DEFAULT_ORDER`。

    ⚠️ 抽不到就要失敗，不可以視為「沒有值＝一致」—— 那會讓這條測試在檔案
    改寫之後靜默失效（`test_enums_sync.py` 有同樣的告誡）。
    """
    src = (ROOT / "snsmediadl/web/js/views/accounts.js").read_text(encoding="utf-8")
    block = re.search(r"const A_DEFAULT_ORDER = \{(.*?)\};", src, re.S)
    assert block, "找不到 A_DEFAULT_ORDER —— accounts.js 改寫了但這條測試沒跟著改"
    pairs = re.findall(r"(\w+):\s*'(asc|desc)'", block.group(1))
    assert pairs, "A_DEFAULT_ORDER 抽出來是空的"
    return dict(pairs)


def _backend_default_desc() -> dict[str, bool]:
    """抽 `query.py` 的 `sorts` 表：`"key": (欄位, True/False)`。"""
    src = (ROOT / "snsmediadl/api/query.py").read_text(encoding="utf-8")
    block = re.search(r"\n    sorts = \{(.*?)\n    \}", src, re.S)
    assert block, "找不到 sorts 表 —— query.py 改寫了但這條測試沒跟著改"
    pairs = re.findall(r'"(\w+)":\s*\([^,]+,\s*(True|False)\)', block.group(1))
    assert pairs, "sorts 抽出來是空的"
    return {k: v == "True" for k, v in pairs}


def test_frontend_sort_keys_match_backend():
    assert sorted(_js_default_order()) == sorted(_backend_default_desc())


def test_default_order_table_matches_backend():
    """箭頭的初始方向必須等於後端不帶 `order` 時真的會做的事。

    對不齊不會報錯，只會讓箭頭指著一個不是實際順序的方向。
    """
    js = _js_default_order()
    be = _backend_default_desc()

    mismatched = {
        k: (js[k], "desc" if be[k] else "asc")
        for k in be if js.get(k) != ("desc" if be[k] else "asc")
    }
    assert not mismatched, f"前端 vs 後端預設方向不一致：{mismatched}"


def test_frontend_sort_keys_match_this_test_file():
    """這份測試自己的 SORT_KEYS 也要跟著 —— 否則新增排序鍵時，
    上面那組 parametrize 會靜默地少測一個。"""
    assert sorted(SORT_KEYS) == sorted(_backend_default_desc())
