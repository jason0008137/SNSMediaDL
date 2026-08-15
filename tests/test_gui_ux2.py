"""UIUX 2.0 需要的後端行為。

這些端點不是為了「多一個 API」而加的，每一支都對應介面上一句**答不出來就會
騙人**的話（見 wiki 的 UI_* 線框筆記）：

  · `hidden_by_safe_mode` → 「共 0 個媒體」旁邊那句「另有 N 筆因安全模式隱藏」
  · `unsupported_platform` → 解析結果表要分辨「貼對了但要換工具」與「打錯字」
  · `refresh-preview`      → 「可抓 N 個 / 不可抓 M 個」必須在按下去**之前**看見
  · `last_ingest`          → 背景活動區的第三條流程（extension 採集）
  · `siblings`             → 詳情面板的「改這裡會套用到全部 N 張」
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Account
from snsmediadl.urls import ParseError, UnsupportedTarget, parse_lines, parse_target


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture()
def loaded(client, sample_account):
    client.post("/api/ingest", json={"platform": "x", "screenName": "sample_account",
                                     "posts": sample_account})


# ── 安全模式擋掉幾筆 ──────────────────────────────────────


def _tag_all_r18(client):
    ids = [m["post_id"] for m in client.get("/api/media?limit=500").json()["items"]]
    client.post("/api/posts/bulk-tags", json={"post_ids": list(set(ids)), "rating": "r18"})


def test_hidden_count_is_only_paid_for_when_the_result_is_empty(client, loaded):
    """⚠️ 這是**第二次 COUNT**，成本翻倍 —— 只在結果為 0 時才算。

    有結果的時候回 `None`。`None` 的意思是「沒去算」，不是「沒有被擋掉的」，
    呼叫端必須分得出來。
    """
    body = client.get("/api/media/count?exclude_rating=r18").json()
    assert body["total"] == 6
    assert body["hidden_by_safe_mode"] is None

    _tag_all_r18(client)

    body = client.get("/api/media/count?exclude_rating=r18").json()
    assert body["total"] == 0
    # 這正是實測抓到的情境：帳號頁寫著「6 個媒體」，點進來卻是空的
    assert body["hidden_by_safe_mode"] == 6


def test_hidden_count_stays_none_without_safe_mode(client, loaded):
    _tag_all_r18(client)
    body = client.get("/api/media/count?rating=sfw").json()
    assert body["total"] == 0
    # 沒開安全模式就沒有「被安全模式擋掉」這回事，不可以回一個數字
    assert body["hidden_by_safe_mode"] is None


def test_hidden_count_respects_the_other_filters(client, loaded):
    _tag_all_r18(client)
    body = client.get("/api/media/count?exclude_rating=r18&kind=photo").json()
    photos = client.get("/api/media/count?kind=photo").json()["total"]
    assert body["total"] == 0
    assert body["hidden_by_safe_mode"] == photos


# ── 「貼對了但要換工具」與「打錯字」是兩種結論 ────────────


def test_unsupported_host_raises_a_distinguishable_error():
    with pytest.raises(UnsupportedTarget) as exc:
        parse_target("https://x.com/someone")
    assert exc.value.platform == "x"
    # 仍然是 ParseError 的子類 —— 既有的 except ParseError 不會漏接
    assert isinstance(exc.value, ParseError)

    with pytest.raises(ParseError) as plain:
        parse_target("ttps://typo.example")
    assert not isinstance(plain.value, UnsupportedTarget)


def test_parse_lines_marks_the_platform_that_needs_another_tool():
    lines = parse_lines("https://x.com/a\nttps://typo.example\nhttps://misskey.io/@b")
    assert [ln.unsupported_platform for ln in lines] == ["x", None, None]
    # 兩者都有 error（都不能排入），差別在於**怎麼跟使用者說**
    assert lines[0].error and lines[1].error and not lines[2].error


def test_parse_endpoint_exposes_the_flag(client):
    body = client.post("/api/fetch/parse",
                       json={"text": "https://x.com/a\nttps://typo.example"}).json()
    assert body["lines"][0]["unsupported_platform"] == "x"
    assert body["lines"][1]["unsupported_platform"] is None


# ── 一鍵更新的規模要在按下去之前看得見 ──────────────────


@pytest.fixture()
def mixed_accounts(session):
    """正式庫的形狀：多數帳號 backend 抓不動（X）。"""
    session.add_all([
        Account(platform="x", instance_host="", platform_user_id="1", screen_name="a"),
        Account(platform="x", instance_host="", platform_user_id="2", screen_name="b"),
        Account(platform="misskey", instance_host="misskey.io",
                platform_user_id="3", screen_name="c"),
        Account(platform="mastodon", instance_host="baraag.net",
                platform_user_id="4", screen_name="d"),
        Account(platform="pixiv", instance_host="", platform_user_id="5", screen_name="e"),
    ])
    session.commit()


def test_refresh_preview_counts_without_queueing_anything(client, mixed_accounts):
    body = client.get("/api/fetch/refresh-preview").json()
    assert body["fetchable"] == 2
    assert body["by_platform"] == {"misskey": 1, "mastodon": 1}
    # 抓不動的要**逐類**帶出來，只給一個總數的話使用者會以為 X 也更新過了
    assert body["skipped"]["cannot_fetch"] == 2
    assert body["skipped"]["pixiv_excluded"] == 1

    # ⚠️ 預覽**不排任何東西**。與刪除預演同一個道理。
    assert client.get("/api/fetch/queue").json()["counts"]["queued"] == 0


def test_refresh_preview_follows_the_include_pixiv_switch(client, mixed_accounts):
    body = client.get("/api/fetch/refresh-preview?include_pixiv=true").json()
    # 沒有憑證時 pixiv 仍然抓不動 —— 但理由不同，要分開講
    assert "pixiv_excluded" not in body["skipped"]
    assert body["skipped"]["no_credentials"] == 1


def test_refresh_preview_min_seconds_is_a_floor_not_an_estimate(client, mixed_accounts, cfg):
    body = client.get("/api/fetch/refresh-preview").json()
    # 兩個 Fediverse 帳號 × 每個至少一個請求
    assert body["min_seconds"] == round(2 * cfg.fetch_delay_seconds)


# ── 背景活動區的第三條流程 ────────────────────────────────


def test_queue_status_reports_the_last_extension_ingest(client, sample_account):
    before = client.get("/api/queue/status").json()
    # 沒收到過就是 None，**不是 0 也不是空字典** —— 介面要能說
    # 「自 backend 啟動以來沒有收到」，那與「收到 0 則」是兩件事
    assert before["last_ingest"] is None

    client.post("/api/ingest", json={"platform": "x", "screenName": "someone",
                                     "posts": sample_account})

    after = client.get("/api/queue/status").json()["last_ingest"]
    assert after["platform"] == "x"
    assert after["screen_name"] == "someone"
    assert after["posts_new"] == 4
    assert after["media_new"] == 6
    assert after["at"]


# ── 詳情面板：改分級會影響同貼文的幾張 ───────────────────


def test_media_detail_lists_the_siblings_of_the_same_post(client, loaded):
    media = client.get("/api/media?limit=500").json()["items"]
    # fixture 裡有一則多媒體貼文
    multi = next(m for m in media
                 if sum(1 for x in media if x["post_id"] == m["post_id"]) > 1)

    d = client.get(f"/api/media/{multi['id']}").json()
    ids = [s["id"] for s in d["siblings"]]
    assert multi["id"] in ids
    assert ids == sorted(ids, key=lambda i: next(
        s["ordinal"] for s in d["siblings"] if s["id"] == i))
    assert len(ids) == sum(1 for x in media if x["post_id"] == multi["post_id"])


def test_settings_expose_the_paths_the_panel_shows(client):
    s = client.get("/api/settings").json()
    # 設定面板要把「這些改不動、要編 config.toml 並重啟」講出來，
    # 講之前得先拿得到值
    assert s["thumb_root"].endswith("thumb")
    assert s["fetch_max_pages"] >= 1


# ── 帳號頁的平台篩選 ──────────────────────────────────────


def test_platform_breakdown_for_the_account_filter(client, mixed_accounts):
    """選項要帶筆數 —— 選到 0 筆的平台時，空清單與「篩選壞了」長得一樣。"""
    items = client.get("/api/accounts/platforms").json()["items"]
    assert {i["platform"]: i["count"] for i in items} == {
        "x": 2, "misskey": 1, "mastodon": 1, "pixiv": 1}
    # 多的排前面：4,211 個 x 要在 9 個 misskey 前面
    assert items[0]["platform"] == "x"


def test_platform_route_is_not_swallowed_by_account_id(client):
    # `/api/accounts/platforms` 不可以被 `/accounts/{account_id}` 之類的路由接走
    assert client.get("/api/accounts/platforms").status_code == 200


def test_accounts_can_be_filtered_by_platform(client, mixed_accounts):
    res = client.get("/api/accounts?platform=mastodon")
    assert [a["screen_name"] for a in res.json()] == ["d"]
    assert res.headers["X-Total-Count"] == "1"
