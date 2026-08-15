"""抓取佇列：序列性、去重、一筆失敗不影響其餘、429 依站台隔離。

一個網路請求都不該發生 —— `fetch_account` 整支被換掉。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from snsmediadl.services import fetch_queue as fq
from snsmediadl.services.fetch import FetchResult
from snsmediadl.urls import Target


def target(platform: str = "misskey", host: str = "misskey.io", acct: str = "a") -> Target:
    return Target(platform=platform, host=host, acct=acct)


async def drain(queue: fq.FetchQueue, timeout: float = 2.0) -> None:
    """等佇列跑完。逾時就失敗，不要讓測試無限掛著。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while queue.status()["counts"]["queued"] or queue.status()["running"]:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"佇列沒跑完：{queue.status()}")
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)   # 讓 _drain() 有機會跑


@pytest.fixture()
def queue(cfg):
    return fq.FetchQueue(cfg=cfg, maker=None)


async def test_runs_one_at_a_time(monkeypatch, queue):
    """⚠️ 序列性是這個模組存在的理由。

    併發列舉同一個站台就是自己把自己打成 429，而 Fediverse 的 429 政策
    是停止不重試 —— 撞上去整批停。
    """
    concurrent = 0
    peak = 0

    async def fake(cfg, maker, **kw):
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.02)
        concurrent -= 1
        return FetchResult(account=kw["acct"])

    monkeypatch.setattr(fq, "fetch_account", fake)
    for name in ("a", "b", "c", "d"):
        queue.enqueue(target(acct=name))
    await drain(queue)

    assert peak == 1
    assert queue.status()["counts"]["done"] == 4


async def test_same_account_is_not_queued_twice(monkeypatch, queue):
    """兩次會列舉同一批，第二次全部撞去重 —— 純粹浪費對方的額度。"""
    async def fake(cfg, maker, **kw):
        await asyncio.sleep(0.05)
        return FetchResult()

    monkeypatch.setattr(fq, "fetch_account", fake)
    assert queue.enqueue(target(acct="a")) is not None
    assert queue.enqueue(target(acct="A")) is None      # 大小寫不敏感
    assert queue.enqueue(target(acct="b")) is not None
    await drain(queue)
    assert queue.status()["counts"]["done"] == 2


async def test_排完之後同一個帳號可以再排(monkeypatch, queue):
    async def fake(cfg, maker, **kw):
        return FetchResult()

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="a"))
    await drain(queue)
    assert queue.enqueue(target(acct="a")) is not None


async def test_one_failure_does_not_stop_the_batch(monkeypatch, queue):
    """批次的重點：一筆壞掉，其餘照跑完，而且看得到是哪一筆壞了。"""
    async def fake(cfg, maker, **kw):
        if kw["acct"] == "bad":
            raise RuntimeError("boom")
        return FetchResult(account=kw["acct"])

    monkeypatch.setattr(fq, "fetch_account", fake)
    for name in ("a", "bad", "c"):
        queue.enqueue(target(acct=name))
    await drain(queue)

    counts = queue.status()["counts"]
    assert (counts["done"], counts["failed"]) == (2, 1)
    failed = [j for j in queue.status()["recent"] if j["state"] == "failed"]
    assert failed[0]["acct"] == "bad"
    assert "boom" in failed[0]["error"]


async def test_404_says_the_account_may_have_been_renamed(monkeypatch, queue):
    """光說 404 沒有用 —— 最常見的原因是改名或打錯字。"""
    async def fake(cfg, maker, **kw):
        raise httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "https://x"),
            response=httpx.Response(404),
        )

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="gone"))
    await drain(queue)
    assert "改名" in queue.status()["recent"][0]["error"]


async def test_429_only_stops_that_host(monkeypatch, queue):
    """⚠️ 限速旗標依站台隔離。

    既有教訓：`WorkerStats.rate_limited` 曾讓 pixiv 的 429 停掉 X 的下載，
    使用者會以為 X 抓完了。同一個錯不要犯第二次。
    """
    async def fake(cfg, maker, **kw):
        if kw["host"] == "misskey.io":
            raise httpx.HTTPStatusError(
                "429", request=httpx.Request("GET", "https://x"),
                response=httpx.Response(429),
            )
        return FetchResult(account=kw["acct"])

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(host="misskey.io", acct="a"))
    queue.enqueue(target(host="misskey.io", acct="b"))       # 同站，應被跳過
    queue.enqueue(target(platform="mastodon", host="baraag.net", acct="c"))
    await drain(queue)

    status = queue.status()
    by_acct = {j["acct"]: j for j in status["recent"]}
    assert by_acct["a"]["state"] == "failed"
    assert by_acct["b"]["state"] == "skipped"
    assert "429" in by_acct["b"]["reason"]
    assert by_acct["c"]["state"] == "done"      # 別的站台不受影響
    assert "misskey@misskey.io" in status["rate_limited"]

    # 解除之後同一個站台又能排了（不自動解除：我們不知道對方的窗口多長）
    queue.clear_rate_limit()
    assert queue.status()["rate_limited"] == {}


async def test_clear_pending_keeps_the_running_one(monkeypatch, queue):
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake(cfg, maker, **kw):
        started.set()
        await release.wait()
        return FetchResult(account=kw["acct"])

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="running"))
    queue.enqueue(target(acct="waiting1"))
    queue.enqueue(target(acct="waiting2"))
    await asyncio.wait_for(started.wait(), 1)

    assert queue.clear_pending() == 2
    release.set()
    await drain(queue)

    by_acct = {j["acct"]: j["state"] for j in queue.status()["recent"]}
    assert by_acct["running"] == "done"          # 跑到一半的讓它跑完
    assert by_acct["waiting1"] == "skipped"


async def test_download_is_triggered_once_after_the_queue_drains(monkeypatch, cfg):
    """抓完就下載是**明確觸發**（queue/run），不是打開 auto_download。"""
    calls = 0

    async def on_drain():
        nonlocal calls
        calls += 1

    async def fake(cfg_, maker, **kw):
        return FetchResult()

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue = fq.FetchQueue(cfg=cfg, maker=None, on_drain=on_drain)
    queue.enqueue(target(acct="a"), download_after=True)
    queue.enqueue(target(acct="b"), download_after=True)
    await drain(queue)
    assert calls == 1
    assert cfg.auto_download is False        # 全程沒有被打開

    # 沒要求下載就不要碰
    queue.enqueue(target(acct="c"))
    await drain(queue)
    assert calls == 1


async def test_user_id_is_passed_through(monkeypatch, queue):
    """更新既有帳號要用平台 user id，不用會改的 screen_name。"""
    seen = {}

    async def fake(cfg, maker, **kw):
        seen.update(kw)
        return FetchResult()

    monkeypatch.setattr(fq, "fetch_account", fake)
    queue.enqueue(target(acct="old_name"), user_id="u123")
    await drain(queue)
    assert seen["user_id"] == "u123"
    assert seen["acct"] == "old_name"        # 仍然帶著，只當顯示名


async def test_status_says_the_queue_is_volatile(queue):
    """重啟就沒了，介面必須講明 —— 不然使用者會以為排好的東西還在。"""
    assert queue.status()["volatile"] is True


# ── 錯誤訊息要帶得出平台講的原因 ──────────────────────────


async def test_http_error_carries_the_platform_message(monkeypatch, queue, cfg):
    """400 的 body 裡有原因，丟掉它等於逼使用者去猜。

    有前科：8 個 misskey 帳號回 400，被猜成「掃太快」，實際上是拿 `sn:` 哨符
    當 userId 去查（見 `services/identity.py`）。當時畫面上只有「HTTP 400」。
    """
    async def boom(*a, **kw):
        raise httpx.HTTPStatusError(
            "400", request=httpx.Request("POST", "https://misskey.io/api/users/show"),
            response=httpx.Response(
                400, json={"error": {"code": "INVALID_PARAM", "message": "userId is invalid"}}),
        )

    monkeypatch.setattr(fq, "fetch_account", boom)
    monkeypatch.setattr(fq.FetchQueue, "_record", lambda self, *a, **kw: _noop())
    job = queue.enqueue(target())
    await drain(queue)
    assert job.state == "failed"
    assert "INVALID_PARAM" in job.error
    assert "userId is invalid" in job.error


async def test_error_detail_never_raises_on_a_non_json_body():
    """這個函式跑在錯誤處理路徑上 —— 它自己炸掉會把原本的錯誤蓋掉。"""
    html = httpx.Response(502, text="<html><body>Bad Gateway</body></html>")
    assert "Bad Gateway" in fq._error_detail(html)
    assert fq._error_detail(httpx.Response(500, text="")) == ""


async def _noop():
    return None


# ── 帳號之間的節流 ────────────────────────────────────────


async def test_pacing_between_accounts(monkeypatch, cfg):
    """兩個帳號之間要有間隔。

    ⚠️ 這**不是** HTTP 400 的解方（那是哨符 id），是對站台的禮貌 ——
    序列佇列原本帳號與帳號之間完全沒有間隔，實測 200 ms 一個。
    """
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)

    cfg.fetch_account_delay_seconds = 1.5
    queue = fq.FetchQueue(cfg=cfg, maker=None, autostart=False)

    async def ok(*a, **kw):
        return FetchResult(account="a")

    monkeypatch.setattr(fq, "fetch_account", ok)
    monkeypatch.setattr(fq.FetchQueue, "_record", lambda self, *a, **kw: _noop())
    monkeypatch.setattr(fq.asyncio, "sleep", fake_sleep)

    queue.enqueue(target(acct="a"))
    queue.enqueue(target(acct="b"))
    await queue.run_all()
    assert slept.count(1.5) == 2      # 每個 job 之後各一次

    slept.clear()
    cfg.fetch_account_delay_seconds = 0
    queue2 = fq.FetchQueue(cfg=cfg, maker=None, autostart=False)
    queue2.enqueue(target(acct="c"))
    await queue2.run_all()
    assert 0 not in slept             # 關掉就完全不等
