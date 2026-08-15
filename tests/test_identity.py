"""匯入來的帳號身分：`sn:` 哨符不可以當成真的平台 id。

背景（2026-08-15，正式庫實跑）：「一鍵更新」讓 8 個 misskey 帳號回 HTTP 400，
當下被讀成「掃太快，要加限速」。實際上是 `plan_refresh` 把匯入器造的哨符
`sn:<name>` 原封不動當成 userId 送去查 —— 9 個帳號裡唯一有真實 id 的那個
抓成功，8 個哨符全部失敗，而且成功的那次還跑在失敗那批之後。

限速的特徵是「前面成功、後面開始失敗」；這裡是依帳號決定的，跟順序無關。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from snsmediadl.db.models import Account, Media, Post
from snsmediadl.services import counters
from snsmediadl.services.fetch import fetch_account
from snsmediadl.services.fetch_queue import plan_refresh
from snsmediadl.services.identity import (
    heal_placeholder_account,
    is_placeholder,
    placeholder_id,
)


@pytest.fixture()
def client_app(cfg, session):
    """API + 同一個 session。`/api/ingest` 那條路徑要用真的端點驗。"""
    from fastapi.testclient import TestClient
    from snsmediadl.api.app import create_app, get_session

    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app), session


def _account(session, platform, user_id, screen, host="", **kw):
    a = Account(platform=platform, instance_host=host,
                platform_user_id=user_id, screen_name=screen, **kw)
    session.add(a)
    session.flush()
    return a


def _post(session, account, post_id, platform=None, host=""):
    p = Post(platform=platform or account.platform, instance_host=host,
             platform_post_id=post_id, account_id=account.id)
    session.add(p)
    session.flush()
    session.add(Media(post_id=p.id, ordinal=0, kind="photo",
                      platform_media_key=f"{post_id}-0",
                      source_url=f"https://x/{post_id}.jpg", status="done"))
    session.flush()
    return p


# ── 哨符的判定 ────────────────────────────────────────────


def test_placeholder_is_recognised():
    assert is_placeholder("sn:had")
    assert not is_placeholder("9dsepv1hly")
    assert not is_placeholder(None)
    assert placeholder_id("had") == "sn:had"


# ── plan_refresh 不把哨符當 id ────────────────────────────


def test_plan_refresh_does_not_send_placeholder_as_user_id(session, cfg):
    _account(session, "misskey", "sn:had", "had", host="misskey.io")
    _account(session, "misskey", "9dsepv1hly", "PeachYP", host="misskey.io")
    session.commit()

    by_name = {t.acct: uid for t, uid in plan_refresh(session, cfg).targets}
    # 哨符 → None（改走名字解析）。送出去的話 misskey 回 400。
    assert by_name["had"] is None
    # 真實 id 一定要帶著 —— 帳號改名是常態，拿舊名字去查會 404
    assert by_name["PeachYP"] == "9dsepv1hly"


def test_plan_refresh_includes_mastodon(session, cfg):
    """平台名要與 adapter 註冊表一致，否則整批被歸成「抓不動」。

    匯入器原本寫的是 `baraag`（EpicDL 的目錄名），12 個帳號因此在「一鍵更新」
    裡靜默消失 —— 而畫面給的理由還是錯的（`cannot_fetch` 的文案是為 X 寫的）。
    """
    _account(session, "mastodon", "1", "artist", host="baraag.net")
    _account(session, "baraag", "2", "old", host="baraag.net")
    session.commit()

    plan = plan_refresh(session, cfg)
    assert [t.acct for t, _ in plan.targets] == ["artist"]
    assert plan.skipped["cannot_fetch"] == ["@old@baraag.net"]


# ── 治好哨符 ──────────────────────────────────────────────


def test_heal_renames_in_place_keeping_posts(session):
    """沒有真 id 列時：改同一列。**列的 id 不變，貼文與媒體一筆不掉。**"""
    ghost = _account(session, "misskey", "sn:had", "had", host="misskey.io",
                     is_favorite=True, stars=4)
    _post(session, ghost, "p1", host="misskey.io")
    _post(session, ghost, "p2", host="misskey.io")
    session.commit()
    ghost_id = ghost.id

    healed = heal_placeholder_account(
        session, "misskey", "misskey.io", screen_name="had", real_id="9real")
    session.commit()

    assert healed.id == ghost_id                 # 同一列
    assert healed.platform_user_id == "9real"
    assert healed.is_favorite is True            # 使用者標過的東西不能掉
    assert healed.stars == 4
    assert session.query(Post).filter_by(account_id=ghost_id).count() == 2
    assert session.query(Account).count() == 1


def test_heal_merges_when_the_real_row_already_exists(session):
    """兩列都在時：合併。

    這不是假想情況 —— 正式庫的 pixiv `東西` 就是 `sn:東西`（23 個媒體）
    ＋ `16347608`（5 個媒體）兩列並存。不合併的話，之後每次抓取都只餵真 id
    那一列，匯入來的 23 個永遠留在另一邊，而畫面上看不出有兩列。
    """
    ghost = _account(session, "pixiv", "sn:東西", "東西", default_rating="r18")
    real = _account(session, "pixiv", "16347608", "東西")
    _post(session, ghost, "old1")
    _post(session, ghost, "old2")
    _post(session, real, "new1")
    counters.recompute(session)
    session.commit()
    ghost_id, real_id = ghost.id, real.id

    healed = heal_placeholder_account(
        session, "pixiv", "", screen_name="東西", real_id="16347608")
    session.commit()

    assert healed.id == real_id
    assert session.get(Account, ghost_id) is None
    # 三則貼文全部掛到留下來的那一列，媒體跟著貼文走
    assert session.query(Post).filter_by(account_id=real_id).count() == 3
    assert session.query(Media).count() == 3
    assert healed.post_count == 3           # 聚合欄有重算
    assert healed.media_count == 3
    # 真 id 那列沒設過的偏好，從哨符列繼承
    assert healed.default_rating == "r18"


def test_heal_is_a_noop_without_a_placeholder(session):
    _account(session, "misskey", "9real", "had", host="misskey.io")
    session.commit()
    assert heal_placeholder_account(
        session, "misskey", "misskey.io", screen_name="had", real_id="9real") is None
    assert session.query(Account).count() == 1


def test_heal_refuses_a_placeholder_as_the_real_id(session):
    # 平台不可能回一個哨符。走到這裡代表呼叫端把哨符又傳回來了 —— 要炸，
    # 不可以默默寫進去（那會讓「治好了」與「沒治好」再也分不出來）
    with pytest.raises(ValueError):
        heal_placeholder_account(
            session, "misskey", "misskey.io", screen_name="had", real_id="sn:had")


# ── 端對端：抓取時哨符會被治好，而且不會多出一列 ──────────


def _misskey_transport(calls: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {}
        if request.content:
            import json
            body = json.loads(request.content)
        calls.append({"url": str(request.url), "body": body})
        if request.url.path == "/api/users/show":
            if "userId" in body:
                # 這正是正式庫上發生的事：哨符送過來，misskey 回 400
                return httpx.Response(400, json={
                    "error": {"code": "INVALID_PARAM", "message": "userId is invalid"}})
            return httpx.Response(200, json={"id": "9real", "username": body["username"]})
        if request.url.path == "/api/users/notes":
            return httpx.Response(200, json=[])
        raise AssertionError(f"沒預期到的請求：{request.url}")

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_fetching_a_placeholder_account_heals_it_without_duplicating(
    cfg, engine, session
):
    ghost = _account(session, "misskey", "sn:had", "had", host="misskey.io")
    _post(session, ghost, "p1", host="misskey.io")
    session.commit()
    ghost_id = ghost.id

    calls: list[dict] = []
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    await fetch_account(
        cfg, maker, platform="misskey", host="misskey.io", acct="had",
        user_id=None, transport=_misskey_transport(calls),
    )

    # 一次都不可以用哨符去查
    assert not any("sn:" in str(c["body"].get("userId", "")) for c in calls)
    session.expire_all()
    assert session.query(Account).count() == 1        # **沒有多出第二列**
    healed = session.get(Account, ghost_id)
    assert healed.platform_user_id == "9real"
    assert session.query(Post).filter_by(account_id=ghost_id).count() == 1


def test_heal_matches_the_placeholder_case_insensitively(session):
    """匯入時的名字取自**檔名**，平台回的是它自己的正規化寫法。

    正式庫實測：`sn:Lbf5n`（匯入）與 `lbf5n`（平台）並存。精確比對的話
    這種帳號永遠治不好，而且失敗是靜默的 —— 只是「合併沒發生」。
    """
    ghost = _account(session, "x", "sn:Lbf5n", "Lbf5n")
    _post(session, ghost, "p1")
    session.commit()

    healed = heal_placeholder_account(
        session, "x", "", screen_name="lbf5n", real_id="123456")
    session.commit()

    assert healed is not None, "大小寫不同就找不到 —— 那是靜默失敗"
    assert healed.id == ghost.id
    assert healed.platform_user_id == "123456"
    assert session.query(Account).count() == 1


# ── 增量下載**不依賴**合併 ────────────────────────────────


def test_imported_posts_are_recognised_regardless_of_which_account_row(client_app):
    """匯入過的貼文不會被重抓 —— 而且這件事**與帳號身分無關**。

    去重鍵是 `(platform, instance_host, platform_post_id)`，**不含 account_id**。
    所以即使匯入的貼文掛在 `sn:<name>` 那一列底下，extension 問
    `/api/known` 時一樣認得出來，只有真正新的才會被送。

    ⚠️ 這條測試的用途是**擋住「把 account_id 加進去重鍵」這個念頭**。
    加進去的話，每個匯入帳號的東西會全部重抓一遍（正式庫是 224 萬筆媒體）。
    """
    client, session = client_app
    ghost = _account(session, "x", "sn:someone", "someone")
    _post(session, ghost, "1000000000000000001")
    _post(session, ghost, "1000000000000000002")
    session.commit()

    known = client.get(
        "/api/known?platform=x&post_ids=1000000000000000001,1000000000000000002,999"
    ).json()["known"]
    assert sorted(known) == ["1000000000000000001", "1000000000000000002"]

    # 真的送一次也一樣：舊的跳過，只有新的進來
    body = client.post("/api/ingest", json={
        "platform": "x", "screenName": "someone",
        "posts": [
            {"postId": "1000000000000000001", "userId": "77", "createdAt": None,
             "media": [{"kind": "photo", "url": "https://x/a.jpg"}]},
            {"postId": "2000000000000000001", "userId": "77", "createdAt": None,
             "media": [{"kind": "photo", "url": "https://x/b.jpg"}]},
        ],
    }).json()
    assert body["posts_skipped"] == 1
    assert body["posts_new"] == 1


def test_ingest_heals_the_placeholder_instead_of_creating_a_second_row(client_app):
    """extension 送資料進來時，匯入的那一列會**就地被補上真實 id**。

    不治療的話同一個人會變成兩列：一列有匯入的歷史、一列有新採集的，
    而帳號頁只會顯示兩張同名卡片，沒有任何提示說它們是同一個人。
    """
    client, session = client_app
    ghost = _account(session, "x", "sn:someone", "someone", is_favorite=True)
    _post(session, ghost, "1000000000000000001")
    # 聚合欄在正式庫是被維護的（匯入的 migration 有 backfill），
    # 回饋訊息「併入 N 則舊記錄」讀的就是它 —— 測試要照著現實佈置
    counters.recompute(session)
    session.commit()
    ghost_id = ghost.id

    body = client.post("/api/ingest", json={
        "platform": "x", "screenName": "someone",
        "posts": [{"postId": "2000000000000000001", "userId": "77", "createdAt": None,
                   "media": [{"kind": "photo", "url": "https://x/b.jpg"}]}],
    }).json()

    session.expire_all()
    assert session.query(Account).count() == 1        # **沒有第二列**
    healed = session.get(Account, ghost_id)
    assert healed.platform_user_id == "77"
    assert healed.is_favorite is True                 # 使用者標過的沒掉
    assert session.query(Post).filter_by(account_id=ghost_id).count() == 2
    # 回應要講出來 —— 這是改動歷史資料歸屬的操作，不可以靜默
    assert body["healed"] == [
        {"screen_name": "someone", "real_id": "77", "posts": 1, "media": 1}
    ]


def test_ingest_without_a_screen_name_does_not_guess(client_app):
    """`screenName` 是整個 request 共用的，可能是 null（CLI 路徑）。

    那時**不可以猜**：沒有名字就沒有比對依據，硬猜會把別人的貼文
    掛到某個同平台的哨符帳號底下。
    """
    client, session = client_app
    _account(session, "x", "sn:someone", "someone")
    session.commit()

    client.post("/api/ingest", json={
        "platform": "x",
        "posts": [{"postId": "3000000000000000001", "userId": "88", "createdAt": None,
                   "media": [{"kind": "photo", "url": "https://x/c.jpg"}]}],
    })
    session.expire_all()
    assert session.query(Account).count() == 2        # 照舊新建，不亂認親


def test_heal_records_are_queryable(client_app):
    """治療紀錄要查得到 —— log 會捲掉，這是回溯歸錯戶的唯一線索。"""
    client, session = client_app
    _account(session, "x", "sn:someone", "someone")
    session.commit()

    before = client.get("/api/identity/heals").json()
    assert before["items"] == []
    assert before["pending"] == 1        # 還有幾個只有名字的

    client.post("/api/ingest", json={
        "platform": "x", "screenName": "someone",
        "posts": [{"postId": "4000000000000000001", "userId": "99", "createdAt": None,
                   "media": [{"kind": "photo", "url": "https://x/d.jpg"}]}],
    })

    after = client.get("/api/identity/heals").json()
    assert after["pending"] == 0
    assert len(after["items"]) == 1
    row = after["items"][0]
    assert (row["screen_name"], row["placeholder_id"], row["real_id"], row["kind"]) == (
        "someone", "sn:someone", "99", "rename")
