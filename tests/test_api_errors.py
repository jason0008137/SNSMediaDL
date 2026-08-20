"""錯誤回應的契約：`code` 給機器，`detail` 給人（而且是英文）。

為什麼值得一整支測試：前端**每一個**錯誤顯示點都吃這個形狀。形狀不一致的
那一次不會炸，只會在某一種錯誤下顯示一片空白 —— 而那種 bug 沒有人回報得出來
（使用者只會說「按了沒反應」）。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session

# 中日文的字。detail 出現這些字就代表有人又把文案寫回後端了。
CJK = re.compile(r"[一-鿿぀-ヿ　-〿＀-￯]")


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


# (方法, 路徑, 預期狀態, 預期 code)
CASES = [
    ("GET", "/api/media/999999", 404, "media.not_found"),
    ("GET", "/api/media?sort=banana", 422, "query.bad_sort"),
    ("GET", "/api/media?sort=added&order=sideways", 422, "query.bad_order"),
    ("GET", "/api/media?cursor=nonsense", 422, "query.bad_cursor"),
    ("GET", "/api/accounts?sort=banana", 422, "query.bad_sort"),
    ("GET", "/api/accounts?fetch_status=banana", 422, "query.bad_fetch_status"),
    ("GET", "/api/creators/999999", 404, "creator.not_found"),
    ("GET", "/api/media/999999/file", 404, "media.not_found"),
    ("GET", "/api/media/999999/thumb", 404, "media.not_found"),
    ("GET", "/api/accounts/999999/deletion-preview", 404, "delete.not_found"),
]


@pytest.mark.parametrize(("method", "path", "status", "code"), CASES)
def test_error_carries_a_code(client, method, path, status, code):
    r = client.request(method, path)
    assert r.status_code == status
    assert r.json()["code"] == code


@pytest.mark.parametrize(("method", "path", "status", "code"), CASES)
def test_detail_is_english(client, method, path, status, code):
    """`detail` 會直接出現在畫面上，而畫面可能是三個語系裡的任何一個。"""
    detail = client.request(method, path).json()["detail"]
    assert detail, f"{path} 回了一個空的 detail"
    found = CJK.findall(detail)
    assert not found, f"{path} 的 detail 裡有中日文字元：{found}"


def test_every_error_body_has_both_fields(client):
    """形狀一致 —— 前端不必為每個端點各防一次。"""
    for method, path, _status, _code in CASES:
        body = client.request(method, path).json()
        assert set(body) == {"code", "detail"}, f"{path} 的形狀是 {sorted(body)}"


def test_unknown_route_also_gets_the_same_shape(client):
    """FastAPI 自己丟的 404 沒有 code。

    ⚠️ 那時 `code` 是 **null**，不是一個編出來的字串。前端看到 null 就退回
    顯示 detail —— 硬編一個假的 code 會讓「這個情況我們還沒分類」看起來像
    「我們已經處理了」。
    """
    r = client.get("/api/nope-nope-nope")
    assert r.status_code == 404
    assert r.json() == {"code": None, "detail": "Not Found"}


def test_bulk_prefs_without_fields_says_which_problem_it_is(client):
    """「沒選帳號」與「沒選要改什麼」是兩件事，前端要分得開。"""
    r = client.post("/api/accounts/bulk-prefs", json={"ids": []})
    assert r.json()["code"] == "bulk.no_ids"

    r = client.post("/api/accounts/bulk-prefs", json={"ids": [1]})
    assert r.json()["code"] == "bulk.nothing_to_change"
