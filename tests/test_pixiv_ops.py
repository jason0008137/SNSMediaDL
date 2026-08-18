"""pixiv 的「找不到」與自動退訂。

⭐ 核心性質：**帳號被刪掉與 pixiv 改版不可以混為一談。**
自動退訂建立在「找不到」這個判斷上；判寬了，pixiv 改一次欄位名就會把
一整批還活著的帳號退訂掉 —— 那比不退訂糟得多。

判定形狀由 recon 驗證（2026-08-18，見「pixiv Recon - Cloudflare 指紋與 ajax API」
的『帳號不存在長什麼樣』）：**HTTP 404 + 合法 JSON + `error: true`**。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from snsmediadl.adapters.pixiv import (
    PixivAdapter,
    PixivFieldError,
    PixivNotFound,
    raise_if_not_found,
)
from snsmediadl.db.enums import FetchStatus
from snsmediadl.db.models import Account
from snsmediadl.services import fetch_queue as fq
from snsmediadl.services.fetch import FetchResult


# ── T2：什麼算「不存在」，什麼不算 ─────────────────────

def _response(status: int, *, json_body=None, text: str | None = None) -> httpx.Response:
    req = httpx.Request("GET", "https://www.pixiv.net/ajax/user/999999999")
    if json_body is not None:
        return httpx.Response(status, json=json_body, request=req)
    return httpx.Response(status, text=text or "", request=req)


def test_verified_shape_raises_not_found():
    """recon 實測到的形狀：404 + `error: true` + `body: []`。"""
    r = _response(404, json_body={"error": True, "message": "使用者已離開 pixiv",
                                  "body": []})
    with pytest.raises(PixivNotFound):
        raise_if_not_found(r, "ajax/user/999999999")


def test_html_404_is_not_treated_as_missing_account():
    """⭐ HTML 的 404 = 端點沒了 = **平台改版**。

    當成「帳號不存在」的話，pixiv 改版當天所有 pixiv 帳號都會連續兩輪
    404，然後整批被自動退訂。這一條就是防那件事。
    """
    r = _response(404, text="<!DOCTYPE html><html><title>404</title>")
    raise_if_not_found(r, "ajax/user/1")     # 不丟例外
    # 它仍然是個錯誤，只是要走 raise_for_status → 泛用 FAILED / 404 那條路
    with pytest.raises(httpx.HTTPStatusError):
        r.raise_for_status()


def test_403_challenge_is_not_missing_account():
    """Cloudflare 擋人是 403 挑戰頁，連 404 都不是。"""
    r = _response(403, text="<!DOCTYPE html><title>Just a moment...</title>")
    raise_if_not_found(r, "ajax/user/1")


def test_200_with_error_is_not_missing_account():
    """200 + `error: true` 是**別種**錯誤（例如缺欄位、權限）。

    帳號不存在實測是 404；把 200 也算進來就是在猜。
    """
    r = _response(200, json_body={"error": True, "message": "something else"})
    raise_if_not_found(r, "ajax/user/1")


def test_not_found_is_a_field_error_subclass():
    """既有的 `except PixivFieldError` 呼叫端不會因為新型別而漏接。"""
    assert issubclass(PixivNotFound, PixivFieldError)


def test_adapter_resolve_account_raises_not_found():
    """走完整條路徑：adapter → 404 JSON → PixivNotFound。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": True, "message": "沒有這個人",
                                         "body": []})

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            await PixivAdapter().resolve_account(client, "", "999999999")

    with pytest.raises(PixivNotFound):
        asyncio.run(go())


def test_adapter_missing_field_is_still_a_plain_field_error():
    """⭐ 改版（缺欄位）**必須**維持 `PixivFieldError` → `failed`。

    與「不存在」混在一起，等於讓改版去觸發退訂。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        # 200、沒有 error，但 body 少了 name —— 典型的改版症狀
        return httpx.Response(200, json={"error": False, "body": {"userId": "1"}})

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            await PixivAdapter().resolve_account(client, "", "1")

    with pytest.raises(PixivFieldError) as exc:
        asyncio.run(go())
    assert not isinstance(exc.value, PixivNotFound)


# ── T4：計數與自動退訂 ────────────────────────────────

@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def pixiv_account(maker) -> Account:
    with maker() as s:
        a = Account(platform="pixiv", instance_host="", platform_user_id="12345",
                    screen_name="作者")
        s.add(a)
        s.commit()
        return a


def run_job(queue: fq.FetchQueue, monkeypatch, outcome, *,
            platform="pixiv", host="", acct="12345", user_id="12345") -> fq.Job:
    async def fake_fetch(*_a, **_kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fq, "fetch_account", fake_fetch)
    job = fq.Job(id=1, platform=platform, host=host, acct=acct, user_id=user_id)
    asyncio.run(queue._process(job))
    return job


def acct_row(maker) -> Account:
    with maker() as s:
        return s.query(Account).one()


def missing() -> PixivNotFound:
    return PixivNotFound("ajax/user/12345：這個 pixiv 使用者不存在")


def test_first_not_found_does_not_untrack(cfg, maker, pixiv_account, monkeypatch):
    """⭐ 一次不算數。手滑打錯 id、平台暫時性故障都會給出一次 404。"""
    q = fq.FetchQueue(cfg=cfg, maker=maker)
    run_job(q, monkeypatch, missing())
    a = acct_row(maker)
    assert a.last_fetch_status == FetchStatus.NOT_FOUND.value
    assert a.not_found_streak == 1
    assert a.is_tracked is True
    assert q.status()["auto_untracked"] == []


def test_second_not_found_untracks(cfg, maker, pixiv_account, monkeypatch):
    q = fq.FetchQueue(cfg=cfg, maker=maker)
    run_job(q, monkeypatch, missing())
    run_job(q, monkeypatch, missing())
    a = acct_row(maker)
    assert a.not_found_streak == 2
    assert a.is_tracked is False
    assert "自動移出追蹤名單" in a.last_fetch_note
    # 使用者看得見 —— 靜默退訂只會讓人覺得帳號自己不見了
    assert len(q.status()["auto_untracked"]) == 1


def test_success_in_between_resets_the_streak(cfg, maker, pixiv_account, monkeypatch):
    """計數是「連續」，不是「累計」。"""
    q = fq.FetchQueue(cfg=cfg, maker=maker)
    run_job(q, monkeypatch, missing())
    run_job(q, monkeypatch, FetchResult(posts_new=3))
    assert acct_row(maker).not_found_streak == 0

    run_job(q, monkeypatch, missing())
    a = acct_row(maker)
    assert a.not_found_streak == 1
    assert a.is_tracked is True


def test_no_new_also_resets(cfg, maker, pixiv_account, monkeypatch):
    """`no_new` 一樣是「這個帳號活著」。"""
    q = fq.FetchQueue(cfg=cfg, maker=maker)
    run_job(q, monkeypatch, missing())
    run_job(q, monkeypatch, FetchResult(posts_new=0))
    assert acct_row(maker).not_found_streak == 0


@pytest.mark.parametrize("outcome", [
    RuntimeError("平台改版了"),                       # → failed
    httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("GET", "https://x.invalid/"),
        response=httpx.Response(
            500, request=httpx.Request("GET", "https://x.invalid/")),
    ),
])
def test_other_failures_do_not_touch_the_streak(
    cfg, maker, pixiv_account, monkeypatch, outcome
):
    """⭐ `failed` 既不加也不減。

    加一 = 讓平台改版去推動退訂；歸零 = 一次限速就把前面兩次真的找不到洗掉。
    """
    q = fq.FetchQueue(cfg=cfg, maker=maker)
    run_job(q, monkeypatch, missing())
    run_job(q, monkeypatch, outcome)
    a = acct_row(maker)
    assert a.not_found_streak == 1
    assert a.is_tracked is True


def test_generic_404_does_not_count(cfg, maker, pixiv_account, monkeypatch):
    """⭐ 泛用的 HTTP 404 記成 `not_found`，但**不累積退訂計數**。

    只有 recon 驗證過的形狀（`PixivNotFound`）才算數 —— 端點被移掉、
    網址打錯都會給出一個裸的 404。
    """
    req = httpx.Request("GET", "https://www.pixiv.net/")
    bare = httpx.HTTPStatusError(
        "boom", request=req, response=httpx.Response(404, request=req))
    q = fq.FetchQueue(cfg=cfg, maker=maker)
    run_job(q, monkeypatch, bare)
    run_job(q, monkeypatch, bare)
    a = acct_row(maker)
    assert a.last_fetch_status == FetchStatus.NOT_FOUND.value
    assert a.not_found_streak == 0
    assert a.is_tracked is True, "沒驗證過的 404 不可以觸發退訂"


def test_skipped_neither_counts_nor_resets(cfg, maker, pixiv_account, monkeypatch):
    """跳過那一輪根本沒查。"""
    q = fq.FetchQueue(cfg=cfg, maker=maker)
    run_job(q, monkeypatch, missing())
    q._rate_limited[("pixiv", "")] = "稍早回了 429"
    run_job(q, monkeypatch, FetchResult())
    a = acct_row(maker)
    assert a.last_fetch_status == FetchStatus.SKIPPED.value
    assert a.not_found_streak == 1


def test_other_platforms_are_never_auto_untracked(cfg, maker, monkeypatch):
    """⭐ 只做 pixiv。

    Fediverse 的 404 最常見原因是**改名**，而改名有 `sn:` 哨符治療那條路
    （services/identity.py）—— 自動退訂會直接打斷它。
    """
    with maker() as s:
        s.add(Account(platform="misskey", instance_host="misskey.io",
                      platform_user_id="u1", screen_name="someone"))
        s.commit()

    q = fq.FetchQueue(cfg=cfg, maker=maker)
    for _ in range(4):
        run_job(q, monkeypatch, missing(), platform="misskey",
                host="misskey.io", acct="someone", user_id="u1")

    a = acct_row(maker)
    assert a.not_found_streak == 4      # 計數照記（診斷用）
    assert a.is_tracked is True         # 但**不退訂**


def test_already_untracked_account_is_not_reported_again(
    cfg, maker, pixiv_account, monkeypatch
):
    """退訂過的帳號再抓一次，不該又出現在「本輪退訂」名單裡。"""
    q = fq.FetchQueue(cfg=cfg, maker=maker)
    run_job(q, monkeypatch, missing())
    run_job(q, monkeypatch, missing())
    assert len(q.status()["auto_untracked"]) == 1
    run_job(q, monkeypatch, missing())
    assert len(q.status()["auto_untracked"]) == 1


# ── T5：憑證狀態要在動手之前就講 ──────────────────────

@pytest.fixture()
def client(cfg, session):
    from fastapi.testclient import TestClient

    from snsmediadl.api.app import create_app, get_session

    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def test_settings_reports_credential_presence_only(client, cfg):
    """⚠️ 只回布林。PHPSESSID 一旦回到前端就會出現在記憶體與截圖裡。"""
    r = client.get("/api/settings").json()
    assert r["credentials"]["pixiv"] is False

    cfg.platform_credentials = {"pixiv": "super-secret-value"}
    r = client.get("/api/settings").json()
    assert r["credentials"]["pixiv"] is True
    assert "super-secret-value" not in str(r)


def test_parse_flags_pixiv_lines_when_credential_is_missing(client, cfg):
    """看得懂、抓得動，但現在跑一定失敗 —— 與「看不懂」是兩種結論。"""
    body = client.post("/api/fetch/parse", json={
        "text": "https://www.pixiv.net/users/12345\nhttps://misskey.io/@someone",
    }).json()
    pixiv_line, misskey_line = body["lines"]
    assert pixiv_line["error"] is None, "這一行沒有錯，只是憑證沒設"
    assert pixiv_line["needs_credential"] == "pixiv"
    assert misskey_line["needs_credential"] is None

    cfg.platform_credentials = {"pixiv": "x"}
    body = client.post("/api/fetch/parse", json={
        "text": "https://www.pixiv.net/users/12345"}).json()
    assert body["lines"][0]["needs_credential"] is None


# ── T6：看得見、也反悔得了 ────────────────────────────

def test_account_api_exposes_auto_untracked_flag(client, session):
    """⚠️ 由**資料**判定，不靠比對 note 的文字（文案一改就靜默失效）。"""
    a = Account(platform="pixiv", instance_host="", platform_user_id="1",
                screen_name="作者", is_tracked=False, not_found_streak=2)
    session.add(a)
    session.commit()

    row = client.get("/api/accounts").json()[0]
    assert row["auto_untracked"] is True
    assert row["not_found_streak"] == 2


def test_manually_untracked_account_is_not_labelled_automatic(client, session):
    """使用者自己取消追蹤的帳號，不可以顯示成「自動退訂」。"""
    a = Account(platform="pixiv", instance_host="", platform_user_id="1",
                screen_name="作者", is_tracked=False, not_found_streak=0)
    session.add(a)
    session.commit()
    assert client.get("/api/accounts").json()[0]["auto_untracked"] is False


def test_retrack_resets_the_streak(client, session):
    """⭐ 恢復追蹤**必須**連 streak 一起歸零。

    不歸零的話下一次找不到就是第 3 次，馬上又被退訂 ——
    使用者會覺得這個按鈕根本沒有用。
    """
    a = Account(platform="pixiv", instance_host="", platform_user_id="1",
                screen_name="作者", is_tracked=False, not_found_streak=2,
                last_fetch_note="連續 2 次找不到（2026-08-18），已自動移出追蹤名單")
    session.add(a)
    session.commit()

    r = client.patch(f"/api/accounts/{a.id}/prefs", json={"is_tracked": True}).json()
    assert r["is_tracked"] is True
    assert r["not_found_streak"] == 0

    row = client.get("/api/accounts").json()[0]
    assert row["auto_untracked"] is False
    assert row["last_fetch_note"] is None
