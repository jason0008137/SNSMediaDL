"""`POST /api/queue/run` 與佇列單飛。全部走 MockTransport，不打網路。

這一組測試存在的理由（一個真的發生過的 bug）：
extension 上的「送出並下載」只打 `/api/ingest`，而 ingest 只入庫排隊。
在這個端點出現之前，唯一會下載的是 `auto_download` 開著的背景迴圈 ——
按鈕從第一天起就沒有下載過，卻回報成功。
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from snsmediadl.api.app import create_app
from snsmediadl.downloader import runner
from snsmediadl.services.ingest import ingest


def _ok(body: bytes = b"hello-image-bytes"):
    return httpx.MockTransport(lambda req: httpx.Response(200, content=body))


@pytest.fixture(autouse=True)
def _clean_runner():
    runner.reset()
    yield
    runner.reset()


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def client(cfg):
    """`auto_download` 維持預設的關閉，`enable_worker` 也不開 ——
    這正是使用者的實際狀態，端點必須在這個前提下就能下載。

    ⚠️ 這裡刻意**不覆寫** `get_session`：worker 走的是 app 自己的 `_maker`，
    覆寫成 in-memory 的 session fixture 會讓「讀的庫」與「寫的庫」是兩個，
    ingest 進去的資料 worker 根本看不到。

    ⚠️ 一定要用 `with`：TestClient 不當 context manager 用時，**每個 request
    各開一個 event loop 再收掉**，端點 `create_task` 出來的下載會跟著被丟棄。
    """
    app = create_app(cfg, transport=_ok())
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def loaded(client, sample_account):
    client.post("/api/ingest", json={"platform": "x", "screenName": "sample_account",
                                     "posts": sample_account})


def _wait_until_drained(client, timeout: float = 5.0) -> dict:
    """輪詢到佇列清空。每次請求都讓 event loop 有機會推進背景任務。"""
    deadline = time.monotonic() + timeout
    status = client.get("/api/queue/status").json()
    while time.monotonic() < deadline:
        if not status["running"] and status["pending"] == 0:
            return status
        time.sleep(0.02)
        status = client.get("/api/queue/status").json()
    return status


# ── 端點 ──────────────────────────────────────────────

def test_run_downloads_with_auto_download_off(client, loaded):
    """本計畫的核心驗收：背景下載關著，按下去仍然要真的抓到檔。"""
    assert client.get("/api/settings").json()["auto_download"] is False

    r = client.post("/api/queue/run").json()
    assert r["started"] is True
    assert r["pending"] == 6      # sample_account 是 4 posts / 6 media

    status = _wait_until_drained(client)
    assert status["pending"] == 0
    assert status["failed"] == 0
    # `/api/queue/status` 不再數 done（那要掃全表，見該端點的說明）。
    # 要確切知道抓成功幾個就打 count —— 那是明確的一次查詢，不是每 5 秒一次。
    assert client.get("/api/media/count?status=done").json()["total"] == 6


def test_run_writes_real_files(client, loaded, cfg):
    client.post("/api/queue/run")
    _wait_until_drained(client)

    files = [p for p in cfg.output_root.rglob("*") if p.is_file()]
    assert len(files) == 6
    assert all(p.read_bytes() == b"hello-image-bytes" for p in files)
    # .part 是下載中的暫存檔，收工後不該留下
    assert not [p for p in files if p.suffix == ".part"]


def test_run_on_empty_queue_is_not_an_error(client):
    r = client.post("/api/queue/run").json()
    assert r["started"] is True
    assert r["pending"] == 0


def test_status_reports_last_run(client, loaded):
    assert client.get("/api/queue/status").json()["last_run"] is None, \
        "還沒跑過就回報跑過的結果，會讓呼叫端以為下載完成了"

    client.post("/api/queue/run")
    _wait_until_drained(client)

    last = client.get("/api/queue/status").json()["last_run"]
    assert last["done"] == 6
    assert last["finished_at"]


# ── 單飛 ──────────────────────────────────────────────

async def test_second_run_returns_none_instead_of_queueing(cfg, maker, session,
                                                           sample_account):
    """已經在跑就立刻回 None —— 不是排隊等待。

    排隊等待會讓「連按兩次」變成「跑兩輪」，第二輪撿到的是第一輪剛標成
    downloading 的那批，正是要避免的重複下載。
    """
    ingest(session, "x", sample_account, screen_name="a")

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, content=b"x")

    first = asyncio.create_task(
        runner.run_once(cfg, maker, transport=httpx.MockTransport(slow))
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    assert runner.is_running() is True
    assert await runner.run_once(cfg, maker, transport=_ok()) is None

    release.set()
    stats = await first
    assert stats.done == 6


async def test_running_flag_clears_after_failure(cfg, maker, session, sample_account):
    """例外不可以把旗標卡在 True —— 那會讓佇列從此再也跑不動。"""
    ingest(session, "x", sample_account, screen_name="a")

    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("網路爆了")

    with pytest.raises(RuntimeError):
        await runner.run_once(cfg, maker, transport=httpx.MockTransport(boom))

    assert runner.is_running() is False


async def test_no_duplicate_files_when_triggered_repeatedly(cfg, maker, session,
                                                            sample_account):
    """重複觸發不可以產生 `xxx (1).jpg`。

    這是單飛鎖真正要防的事：`_load_pending` 撈 pending，但要進到 `_download_one`
    才標 downloading，兩個 worker 併行會撿到同一批。
    """
    ingest(session, "x", sample_account, screen_name="a")

    await asyncio.gather(*(
        runner.run_once(cfg, maker, transport=_ok()) for _ in range(4)
    ))

    files = [p for p in cfg.output_root.rglob("*") if p.is_file()]
    assert len(files) == 6, [p.name for p in files]
    assert not [p for p in files if "(1)" in p.name]
