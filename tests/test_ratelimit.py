"""下載節流與 429 處理。

X 超速會鎖整個帳號約一天，所以這兩件事是保護措施不是最佳化。
"""

from __future__ import annotations

import dataclasses
import time

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from snsmediadl.db.models import Media
from snsmediadl.downloader import run_worker
from snsmediadl.downloader.worker import Throttle
from snsmediadl.services.ingest import ingest


def _payload(n: int) -> list[dict]:
    return [{
        "postId": f"p{i}", "userId": "u1",
        "createdAt": "Tue Jul 08 11:43:52 +0000 2025",
        "media": [{"kind": "photo", "url": f"https://x/{i}.jpg",
                   "orig": f"https://x/{i}.jpg?name=orig"}],
    } for i in range(n)]


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _ok():
    return httpx.MockTransport(lambda req: httpx.Response(200, content=b"x"))


# ── 節流 ──────────────────────────────────────────────

async def test_throttle_spaces_out_starts():
    t = Throttle(0.15)
    start = time.perf_counter()
    for _ in range(3):
        await t.wait()
    elapsed = time.perf_counter() - start
    # 第一次不等，後兩次各等 0.15
    assert elapsed >= 0.28, f"節流沒生效，只花了 {elapsed:.3f}s"


async def test_throttle_zero_is_free():
    t = Throttle(0)
    start = time.perf_counter()
    for _ in range(20):
        await t.wait()
    assert time.perf_counter() - start < 0.1


async def test_downloads_are_throttled(cfg, session, maker):
    cfg = dataclasses.replace(cfg, download_delay_seconds=0.2, concurrency=4)
    ingest(session, "x", _payload(3), screen_name="acct")

    start = time.perf_counter()
    stats = await run_worker(cfg, maker, transport=_ok())
    elapsed = time.perf_counter() - start

    assert stats.done == 3
    # 3 個下載 -> 至少 2 段間隔
    assert elapsed >= 0.4, f"3 個下載只花了 {elapsed:.3f}s，節流沒生效"


async def test_skipped_items_do_not_consume_throttle(cfg, session, maker):
    """已存在的檔案沒有打網路，不該被節流拖慢。"""
    fast = dataclasses.replace(cfg, download_delay_seconds=0.0)
    ingest(session, "x", _payload(3), screen_name="acct")
    await run_worker(fast, maker, transport=_ok())

    for m in session.scalars(select(Media)):
        m.status = "pending"
    session.commit()

    slow = dataclasses.replace(cfg, download_delay_seconds=1.0)
    start = time.perf_counter()
    stats = await run_worker(slow, maker, transport=_ok())
    elapsed = time.perf_counter() - start

    assert stats.skipped == 3
    assert elapsed < 0.5, f"略過的項目被節流了，花了 {elapsed:.3f}s"


# ── 429 ───────────────────────────────────────────────

async def test_429_stops_the_run(cfg, session, maker):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(429, headers={"retry-after": "900"})

    ingest(session, "x", _payload(5), screen_name="acct")
    stats = await run_worker(cfg, maker, transport=httpx.MockTransport(handler))

    assert stats.rate_limited is True
    assert stats.done == 0
    # 被限速後不該繼續打；併發是 2，所以最多兩個工作已經在途中
    assert len(calls) <= cfg.concurrency, f"限速後還打了 {len(calls)} 次"


async def test_429_leaves_media_pending_not_failed(cfg, session, maker):
    """檔案沒壞，只是現在不能抓 —— 標成 failed 會誤導。"""
    ingest(session, "x", _payload(1), screen_name="acct")
    await run_worker(
        cfg, maker,
        transport=httpx.MockTransport(lambda r: httpx.Response(429)))

    m = session.scalar(select(Media))
    session.refresh(m)
    assert m.status == "pending"
    assert "429" in m.error


async def test_429_records_retry_after(cfg, session, maker):
    ingest(session, "x", _payload(1), screen_name="acct")
    await run_worker(
        cfg, maker,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"retry-after": "900"})))

    m = session.scalar(select(Media))
    session.refresh(m)
    assert "900" in m.error


async def test_429_is_not_retried(cfg, session, maker):
    """自動重試正是把「暫時限速」變成「帳號鎖定」的方式。"""
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429)

    ingest(session, "x", _payload(1), screen_name="acct")
    await run_worker(cfg, maker, transport=httpx.MockTransport(handler))

    assert len(calls) == 1, f"429 被重試了 {len(calls)} 次"


async def test_500_still_retries_normally(cfg, session, maker):
    """一般錯誤的重試行為不受 429 處理影響。"""
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(500)

    ingest(session, "x", _payload(1), screen_name="acct")
    stats = await run_worker(cfg, maker, transport=httpx.MockTransport(handler))

    assert stats.rate_limited is False
    assert stats.failed == 1
    assert len(calls) == cfg.max_attempts
