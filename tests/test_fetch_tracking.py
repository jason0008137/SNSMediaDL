"""最後一次擷取的記錄。

⭐ 核心性質：**失敗時一樣要寫入 `last_fetched_at`。**
記成功時間的話，一個連續失敗三個月的帳號會顯示「三個月前」，跟一個
三個月沒查過的帳號無法區分 —— 而那正是這幾個欄位要分辨的兩件事。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from snsmediadl.adapters import AuthRequired
from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.enums import FetchStatus
from snsmediadl.db.models import Account
from snsmediadl.services import fetch_queue as fq
from snsmediadl.services.fetch import FetchResult


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def account(maker) -> Account:
    with maker() as s:
        a = Account(platform="misskey", instance_host="misskey.io",
                    platform_user_id="u1", screen_name="someone")
        s.add(a)
        s.commit()
        return a


def make_queue(cfg, maker) -> fq.FetchQueue:
    return fq.FetchQueue(cfg=cfg, maker=maker, transport=None)


def run_job(queue: fq.FetchQueue, monkeypatch, outcome, *, user_id="u1") -> fq.Job:
    """跑一個 job，`outcome` 是 fetch_account 的替身（回傳值或要丟的例外）。"""
    async def fake_fetch(*_a, **_kw):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(fq, "fetch_account", fake_fetch)
    job = fq.Job(id=1, platform="misskey", host="misskey.io", acct="someone",
                 user_id=user_id)
    asyncio.run(queue._process(job))
    return job


def acct(maker) -> Account:
    with maker() as s:
        return s.query(Account).one()


# ─────────────────────────────────────────── 七種結果

def test_ok_when_new_posts_arrived(cfg, maker, account, monkeypatch):
    q = make_queue(cfg, maker)
    run_job(q, monkeypatch, FetchResult(posts_new=12, media_new=30,
                                        stopped_because="碰到已抓過的貼文（增量）"))
    a = acct(maker)
    assert a.last_fetch_status == FetchStatus.OK.value
    assert a.last_fetch_new_posts == 12
    assert a.last_fetched_at is not None
    assert "增量" in a.last_fetch_note


def test_no_new_is_distinct_from_ok(cfg, maker, account, monkeypatch):
    """兩者都成功，但使用者要知道的是「有沒有東西進來」。"""
    q = make_queue(cfg, maker)
    run_job(q, monkeypatch, FetchResult(posts_new=0, stopped_because="沒有下一頁了"))
    a = acct(maker)
    assert a.last_fetch_status == FetchStatus.NO_NEW.value
    assert a.last_fetch_new_posts == 0


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://example.invalid/")
    return httpx.HTTPStatusError(
        "boom", request=req, response=httpx.Response(code, request=req))


@pytest.mark.parametrize("exc,expected", [
    (_http_error(429), FetchStatus.RATE_LIMITED.value),
    (_http_error(404), FetchStatus.NOT_FOUND.value),
    (_http_error(500), FetchStatus.FAILED.value),
    (AuthRequired("pixiv 需要 PHPSESSID"), FetchStatus.AUTH_REQUIRED.value),
    (RuntimeError("something else"), FetchStatus.FAILED.value),
])
def test_failures_are_recorded(cfg, maker, account, monkeypatch, exc, expected):
    """⭐ 失敗一樣要留下時間戳 —— 那正是最需要被看見的一種。"""
    q = make_queue(cfg, maker)
    run_job(q, monkeypatch, exc)
    a = acct(maker)
    assert a.last_fetch_status == expected
    assert a.last_fetched_at is not None, "失敗沒有記時間，就無法分辨『抓壞了』與『沒查過』"
    assert a.last_fetch_note        # 原因要留著，否則只剩一個「失敗」
    assert a.last_fetch_new_posts is None


def test_skipped_when_host_is_rate_limited(cfg, maker, account, monkeypatch):
    """跳過也算碰過它了 —— 不記的話使用者看到一個很久沒檢查的帳號，
    卻不知道原因是站台被限速。"""
    q = make_queue(cfg, maker)
    q._rate_limited[("misskey", "misskey.io")] = "稍早回了 429"
    run_job(q, monkeypatch, FetchResult())
    a = acct(maker)
    assert a.last_fetch_status == FetchStatus.SKIPPED.value
    assert a.last_fetched_at is not None


def test_missing_account_is_logged_not_silent(cfg, maker, account, monkeypatch, caplog):
    """找不到對應列通常代表 ingest 建成了另一列（`sn:` 暫代 vs 真實 id）。
    那是真的問題，要出聲。"""
    q = make_queue(cfg, maker)
    with caplog.at_level("WARNING"):
        run_job(q, monkeypatch, FetchResult(posts_new=1), user_id="not-in-db")
    assert any("找不到帳號" in r.message for r in caplog.records)


def test_recording_failure_does_not_break_the_fetch(cfg, maker, account, monkeypatch):
    """記錄壞掉不可以連帶弄垮抓取本身。"""
    q = make_queue(cfg, maker)

    def boom(*_a, **_kw):
        raise sqlite_boom

    sqlite_boom = RuntimeError("db exploded")
    monkeypatch.setattr(q, "maker", boom)
    job = run_job(q, monkeypatch, FetchResult(posts_new=3))
    assert job.state == "done"          # 抓取本身仍算成功


# ─────────────────────────────────────────── API

@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture()
def three(session):
    from datetime import datetime
    rows = [
        ("never", None, None),
        ("old", datetime(2026, 1, 1), FetchStatus.NO_NEW.value),
        ("recent", datetime(2026, 8, 1), FetchStatus.NOT_FOUND.value),
    ]
    for i, (name, when, status) in enumerate(rows, start=1):
        session.add(Account(platform="x", platform_user_id=f"u{i}", screen_name=name,
                            last_fetched_at=when, last_fetch_status=status))
    session.commit()


def names(client, query=""):
    return [a["screen_name"] for a in client.get(f"/api/accounts{query}").json()]


def test_sort_last_fetch_puts_never_checked_first(client, three):
    """⚠️ 刻意與其他排序鍵相反：從沒查過的排最前面，不是沉到底。
    「最久沒檢查的排前面」正是這個排序存在的理由。"""
    assert names(client, "?sort=last_fetch") == ["never", "old", "recent"]


def test_filter_by_fetch_status(client, three):
    assert names(client, "?fetch_status=not_found") == ["recent"]
    assert names(client, "?fetch_status=ok") == []


def test_bad_fetch_status_is_422(client, three):
    assert client.get("/api/accounts?fetch_status=nonsense").status_code == 422


def test_account_dict_exposes_the_new_fields(client, three):
    a = next(x for x in client.get("/api/accounts").json() if x["screen_name"] == "recent")
    assert a["last_fetch_status"] == "not_found"
    assert a["last_fetched_at"].startswith("2026-08-01")
    assert "last_fetch_new_posts" in a and "last_fetch_note" in a


def test_fetch_status_accepts_multiple_values(client, three):
    """「只看抓取有問題的」= 四種失敗狀態一起篩，必須在後端做。

    做成前端過濾的話只濾得到當頁 —— 使用者會在一頁全是「從沒檢查過」
    的清單上看到 0 筆，然後以為沒有任何帳號有問題。實測就是這樣錯的。
    """
    assert names(client, "?fetch_status=not_found,failed,rate_limited") == ["recent"]
    assert names(client, "?fetch_status=no_new,not_found") == ["old", "recent"]


def test_fetch_status_rejects_unknown_values_in_the_list(client, three):
    r = client.get("/api/accounts?fetch_status=not_found,nonsense")
    assert r.status_code == 422
    assert "nonsense" in r.text
