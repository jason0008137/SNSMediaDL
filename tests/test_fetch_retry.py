"""抓取失敗的重試與續抓。

一個網路請求都不該發生 —— `fetch_account` 整支被換掉。

⚠️ 這一整組測試守的是同一件事：**「按了重試」與「它真的重試了」是兩件事**。
限速旗標還掛著時，重排的 job 會在 `_process()` 開頭第一行就被標成 skipped。
`will_be_skipped` 沒回對，畫面就會騙人。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from snsmediadl.adapters import AuthRequired
from snsmediadl.db.enums import FetchStatus
from snsmediadl.services import fetch_queue as fq
from snsmediadl.services.fetch import FetchResult
from snsmediadl.urls import Target


def target(platform: str = "misskey", host: str = "misskey.io", acct: str = "a") -> Target:
    return Target(platform=platform, host=host, acct=acct)


async def drain(queue: fq.FetchQueue, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while queue.status()["counts"]["queued"] or queue.status()["running"]:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"佇列沒跑完：{queue.status()}")
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)


@pytest.fixture()
def queue(cfg):
    return fq.FetchQueue(cfg=cfg, maker=None)


def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://misskey.io/api/notes")
    resp = httpx.Response(code, request=req, json={"error": {"code": "X", "message": "y"}})
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def _last(queue: fq.FetchQueue, acct: str) -> dict:
    """歷史裡那個帳號**最新**的一筆。"""
    hits = [j for j in queue.status()["recent"] if j["acct"] == acct]
    assert hits, f"歷史裡找不到 {acct}"
    return max(hits, key=lambda j: j["attempt"])


# ── A：分類要送到前端 ─────────────────────────────────


@pytest.mark.parametrize(
    ("boom", "expected"),
    [
        (_http_error(429), FetchStatus.RATE_LIMITED.value),
        (_http_error(404), FetchStatus.NOT_FOUND.value),
        (_http_error(500), FetchStatus.FAILED.value),
        (AuthRequired("沒有 pixiv 憑證"), FetchStatus.AUTH_REQUIRED.value),
        (RuntimeError("平台改版了"), FetchStatus.FAILED.value),
    ],
)
async def test_fetch_status_reaches_the_frontend(monkeypatch, queue, boom, expected):
    """每種結局的分類都要進 `as_dict()`。

    以前它只寫進 DB 的 `accounts.last_fetch_status`，佇列這邊丟掉了 ——
    於是畫面上五種結局全部壓成一個紅色的 failed，前端要分類就只能去比對
    錯誤字串裡有沒有「429」，而那是平台文案一改就失效的耦合。
    """
    async def fake(cfg, maker, **kw):
        raise boom

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="x"))
    await drain(queue)

    assert _last(queue, "x")["fetch_status"] == expected


async def test_success_is_split_into_ok_and_no_new(monkeypatch, queue):
    async def fake(cfg, maker, **kw):
        return FetchResult(account=kw["acct"], posts_new=3 if kw["acct"] == "new" else 0)

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="new"))
    queue.enqueue(target(acct="same"))
    await drain(queue)

    assert _last(queue, "new")["fetch_status"] == FetchStatus.OK.value
    assert _last(queue, "same")["fetch_status"] == FetchStatus.NO_NEW.value


async def test_skipped_path_also_records_its_status(monkeypatch, queue):
    """⚠️ 限速跳過那條路徑是**提早 return** 的，加欄位時最容易漏。

    而它正是最該被重試的一類 —— 那一輪它根本沒跑過。
    """
    async def fake(cfg, maker, **kw):
        if kw["acct"] == "first":
            raise _http_error(429)
        return FetchResult(account=kw["acct"])

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="first"))
    queue.enqueue(target(acct="second"))
    await drain(queue)

    second = _last(queue, "second")
    assert second["state"] == "skipped"
    assert second["fetch_status"] == FetchStatus.SKIPPED.value
    assert second["retryable"] is True


# ── B：分類挑選 ───────────────────────────────────────


async def test_retry_all_skips_the_ones_that_need_a_fix_first(monkeypatch, queue):
    """缺憑證與找不到**不進**「全部重試」—— 原因沒排除，重試必定同樣失敗。"""
    booms = {
        "cred": AuthRequired("沒有憑證"),
        "gone": _http_error(404),
        "flaky": _http_error(500),
    }

    async def fake(cfg, maker, **kw):
        boom = booms.get(kw["acct"])
        if boom:
            raise boom
        return FetchResult(account=kw["acct"])

    monkeypatch.setattr(fq, "fetch_account", fake)
    for name in ("cred", "gone", "flaky", "fine"):
        queue.enqueue(target(acct=name))
    await drain(queue)

    r = queue.retry_failed()
    assert r["requeued"] == 1
    assert r["jobs"][0]["acct"] == "flaky"

    # 但**單筆**允許 —— 那是使用者覆寫（他剛去設定頁填完憑證）
    cred_id = _last(queue, "cred")["id"]
    assert queue.retry(cred_id)["requeued"] is True


async def test_include_unretryable_opens_the_gate(monkeypatch, queue):
    async def fake(cfg, maker, **kw):
        raise AuthRequired("沒有憑證")

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="cred"))
    await drain(queue)

    assert queue.retry_failed()["requeued"] == 0
    assert queue.retry_failed(include_unretryable=True)["requeued"] == 1


# ── B：限速旗標 ───────────────────────────────────────


async def test_will_be_skipped_warns_before_it_happens(monkeypatch, queue):
    """旗標還掛著就重試 = 那些會直接被跳過。**事前**要講得出數字。

    不講的話，使用者會看到「已排入 N 個」然後幾秒內全部變 ⊘，
    而完全不知道發生了什麼。
    """
    async def fake(cfg, maker, **kw):
        raise _http_error(429)

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="a"))
    await drain(queue)

    r = queue.retry_failed()
    assert r["requeued"] == 1
    assert r["will_be_skipped"] == 1, "旗標還在，這一筆進去也是被跳過"


async def test_clear_rate_limit_happens_before_requeue(monkeypatch, queue):
    """⚠️ 順序不可以顛倒。先排再清的話，中間那段時間窗會讓前幾筆被跳過。"""
    async def fake(cfg, maker, **kw):
        raise _http_error(429)

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="a"))
    await drain(queue)

    r = queue.retry_failed(clear_rate_limit=True)
    assert r["requeued"] == 1
    assert r["will_be_skipped"] == 0
    assert queue.status()["rate_limited"] == {}


# ── B：拒絕要出聲 ─────────────────────────────────────


async def test_retry_refuses_when_already_queued(monkeypatch, queue):
    """靜默少排幾個就是這個專案禁止的靜默漏抓。"""
    async def fake(cfg, maker, **kw):
        raise _http_error(500)

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="a"))
    await drain(queue)

    job_id = _last(queue, "a")["id"]
    assert queue.retry(job_id)["requeued"] is True
    second = queue.retry(job_id)
    assert second["requeued"] is False
    assert "已經在佇列裡" in second["refused_reason"]


def test_retry_refuses_when_it_rolled_out_of_history(queue):
    r = queue.retry(99999)
    assert r["requeued"] is False
    assert "不在佇列歷史裡" in r["refused_reason"]


async def test_attempt_counts_up(monkeypatch, queue):
    async def fake(cfg, maker, **kw):
        raise _http_error(500)

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="a"))
    await drain(queue)
    first = _last(queue, "a")
    assert first["attempt"] == 1

    queue.retry(first["id"])
    await drain(queue)
    second = _last(queue, "a")
    assert second["attempt"] == 2
    assert second["retry_of"] == first["id"]


async def test_retry_all_only_touches_the_latest_attempt(monkeypatch, queue):
    """同一個帳號在歷史裡有好幾筆時，只重排最新那一筆。

    不去重的話一個帳號會被排三次，而 `_active` 只擋得住其中兩次 ——
    剩下那次的 attempt 還會是舊的。
    """
    async def fake(cfg, maker, **kw):
        raise _http_error(500)

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="a"))
    await drain(queue)
    queue.retry(_last(queue, "a")["id"])
    await drain(queue)

    r = queue.retry_failed()
    assert r["requeued"] == 1
    assert r["jobs"][0]["attempt"] == 3


# ── D：續抓 ───────────────────────────────────────────


async def test_only_page_capped_jobs_are_resumable(monkeypatch, queue):
    """撞頁數上限**不是失敗**（state 是 done），所以它是另一個動作。

    id 清單式平台（pixiv）沒有頁數上限的概念，永遠不會 resumable。
    """
    async def fake(cfg, maker, **kw):
        stopped = {
            "big": "達到頁數上限 20 頁 —— 還有更舊的內容。按「繼續抓」從這裡接下去",
            "done": "碰到已抓過的貼文（增量）",
            "pixivish": "全部作品都抓過了（增量）",
        }[kw["acct"]]
        return FetchResult(account=kw["acct"], stopped_because=stopped)

    monkeypatch.setattr(fq, "fetch_account", fake)
    for name in ("big", "done", "pixivish"):
        queue.enqueue(target(acct=name))
    await drain(queue)

    assert _last(queue, "big")["resumable"] is True
    assert _last(queue, "done")["resumable"] is False
    assert _last(queue, "pixivish")["resumable"] is False


async def test_resume_capped_requeues_with_the_resume_flag(monkeypatch, queue):
    seen: list[bool] = []

    async def fake(cfg, maker, **kw):
        seen.append(kw.get("resume", False))
        return FetchResult(
            account=kw["acct"],
            stopped_because="達到頁數上限 20 頁 —— 還有更舊的內容",
        )

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="big"))
    await drain(queue)
    assert seen == [False]

    r = queue.resume_capped()
    assert r["requeued"] == 1
    assert r["jobs"][0]["resume"] is True
    await drain(queue)
    assert seen == [False, True], "續抓那一輪必須帶 resume=True"


async def test_resume_refuses_a_job_that_did_not_hit_the_cap(monkeypatch, queue):
    """沒撞上限就沒有續抓點。**不要默默從第 1 頁抓** ——
    那會讓使用者以為續抓成功了，實際上抓的是早就有的東西。"""
    async def fake(cfg, maker, **kw):
        return FetchResult(account=kw["acct"], stopped_because="碰到已抓過的貼文（增量）")

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="a"))
    await drain(queue)

    r = queue.retry(_last(queue, "a")["id"], resume=True)
    assert r["requeued"] is False
    assert "不需要續抓" in r["refused_reason"]
