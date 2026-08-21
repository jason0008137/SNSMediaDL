"""下載 worker。全部走 MockTransport，不打網路。"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from snsmediadl.db.models import Media
from snsmediadl.fspath import for_io
from snsmediadl.downloader import run_worker
from snsmediadl.services.ingest import ingest

PAYLOAD = [{
    "postId": "p1", "userId": "u1", "createdAt": "Tue Jul 08 11:43:52 +0000 2025",
    "media": [{"kind": "photo", "url": "https://pbs.twimg.com/media/AAA.jpg",
               "orig": "https://pbs.twimg.com/media/AAA.jpg?name=orig"}],
}]


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _ok(body: bytes = b"hello-image-bytes"):
    return httpx.MockTransport(lambda req: httpx.Response(200, content=body))


def _status(code: int):
    return httpx.MockTransport(lambda req: httpx.Response(code, content=b""))


def _boom(exc: Exception):
    def handler(req):
        raise exc
    return httpx.MockTransport(handler)


async def test_downloads_and_marks_done(cfg, session, maker):
    ingest(session, "x", PAYLOAD, screen_name="acct")
    stats = await run_worker(cfg, maker, transport=_ok())

    assert (stats.done, stats.failed) == (1, 0)
    m = session.scalar(select(Media))
    session.refresh(m)
    assert m.status == "done"
    assert m.bytes == len(b"hello-image-bytes")
    assert m.file_hash and len(m.file_hash) == 64
    assert Path(m.local_path).exists()
    assert Path(m.local_path).read_bytes() == b"hello-image-bytes"


async def test_file_lands_in_platform_account_folder(cfg, session, maker):
    ingest(session, "x", PAYLOAD, screen_name="acct")
    await run_worker(cfg, maker, transport=_ok())
    m = session.scalar(select(Media))
    session.refresh(m)
    assert Path(m.local_path).parent == cfg.output_root / "x" / "acct"


async def test_no_part_file_left_behind_on_success(cfg, session, maker):
    ingest(session, "x", PAYLOAD, screen_name="acct")
    await run_worker(cfg, maker, transport=_ok())
    assert list(cfg.output_root.rglob("*.part")) == []


async def test_404_fails_immediately_without_retrying(cfg, session, maker):
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(404)

    ingest(session, "x", PAYLOAD, screen_name="acct")
    stats = await run_worker(cfg, maker, transport=httpx.MockTransport(handler))

    assert (stats.done, stats.failed) == (0, 1)
    assert len(calls) == 1, "4xx 是永久失敗，不該重試"
    m = session.scalar(select(Media))
    session.refresh(m)
    assert m.status == "failed"
    assert "404" in m.error


async def test_500_is_retried_up_to_max_attempts(cfg, session, maker):
    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(500)

    ingest(session, "x", PAYLOAD, screen_name="acct")
    stats = await run_worker(cfg, maker, transport=httpx.MockTransport(handler))

    assert stats.failed == 1
    assert len(calls) == cfg.max_attempts


async def test_network_error_cleans_up_part_file(cfg, session, maker):
    ingest(session, "x", PAYLOAD, screen_name="acct")
    stats = await run_worker(
        cfg, maker, transport=_boom(httpx.ConnectError("boom")))

    assert stats.failed == 1
    assert list(cfg.output_root.rglob("*.part")) == [], "中斷不可留下半截檔"
    assert list(cfg.output_root.rglob("*.jpg")) == [], "也不可留下正式檔"


async def test_attempt_count_recorded(cfg, session, maker):
    ingest(session, "x", PAYLOAD, screen_name="acct")
    await run_worker(cfg, maker, transport=_status(500))
    m = session.scalar(select(Media))
    session.refresh(m)
    assert m.attempt_count == cfg.max_attempts


async def test_second_run_skips_already_downloaded(cfg, session, maker):
    """重跑不重抓 —— 增量是預設行為。"""
    ingest(session, "x", PAYLOAD, screen_name="acct")
    await run_worker(cfg, maker, transport=_ok())

    m = session.scalar(select(Media))
    session.refresh(m)
    m.status = "pending"          # 手動打回佇列，模擬重新排隊
    session.commit()

    calls = []

    def handler(req):
        calls.append(req.url)
        return httpx.Response(200, content=b"different")

    stats = await run_worker(cfg, maker, transport=httpx.MockTransport(handler))
    assert stats.skipped == 1
    assert calls == [], "檔案還在且 hash 相符就不該再打網路"


async def test_missing_file_triggers_redownload(cfg, session, maker):
    ingest(session, "x", PAYLOAD, screen_name="acct")
    await run_worker(cfg, maker, transport=_ok())

    m = session.scalar(select(Media))
    session.refresh(m)
    Path(m.local_path).unlink()
    m.status = "pending"
    session.commit()

    stats = await run_worker(cfg, maker, transport=_ok())
    assert stats.done == 1
    assert Path(m.local_path).exists()


async def test_all_six_real_media_download(cfg, session, maker, sample_account):
    ingest(session, "x", sample_account, screen_name="sample_account")
    stats = await run_worker(cfg, maker, transport=_ok())

    assert (stats.done, stats.failed) == (6, 0)
    files = sorted(p.name for p in cfg.output_root.rglob("*") if p.is_file())
    assert len(files) == 6
    assert all(m.status == "done" for m in session.scalars(select(Media)))


async def test_empty_queue_is_noop(cfg, maker):
    stats = await run_worker(cfg, maker, transport=_ok())
    assert stats.as_dict()["done"] == 0


# ── 長路徑（MAX_PATH）─────────────────────────────────
#
# 現在不會炸，是因為 `output_root` 底下的路徑還短。等某個帳號名或自訂
# filename_format 把它撐過 260，症狀會是「下載回報成功、檢視器卻打不開」——
# 比一開始就失敗更難查。

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="MAX_PATH 是 Windows 的事")


def _deep_root(cfg) -> None:
    """把 `output_root` 墊到夠深，讓最終檔案路徑超過 260 字元。"""
    deep = cfg.output_root
    while len(str(deep)) < 230:
        deep = deep / ("d" * 40)
    cfg.output_root = deep


@WINDOWS_ONLY
async def test_long_target_path_downloads(cfg, session, maker):
    _deep_root(cfg)
    ingest(session, "x", PAYLOAD, screen_name="acct")
    stats = await run_worker(cfg, maker, transport=_ok())

    assert (stats.done, stats.failed) == (1, 0)
    m = session.scalar(select(Media))
    session.refresh(m)
    assert len(m.local_path) > 260, f"沒墊夠長：{len(m.local_path)}"
    assert for_io(m.local_path).read_bytes() == b"hello-image-bytes"


@WINDOWS_ONLY
async def test_long_path_is_stored_without_the_prefix(cfg, session, maker):
    r"""DB 裡永遠不放 `\?\`。存進去的話 GUI、備份、跨機器搬 DB 全都會看到它。"""
    _deep_root(cfg)
    ingest(session, "x", PAYLOAD, screen_name="acct")
    await run_worker(cfg, maker, transport=_ok())

    m = session.scalar(select(Media))
    session.refresh(m)
    assert not m.local_path.startswith("\\?\\")
    assert m.local_path.startswith(str(cfg.output_root))


@WINDOWS_ONLY
async def test_long_path_leaves_no_part_file(cfg, session, maker):
    """`.part` 的建立、寫入、rename、清除是四個獨立的呼叫 —— 漏掉任何一個
    加前綴，這條就會紅（不是留下 .part 就是整個下載失敗）。"""
    _deep_root(cfg)
    ingest(session, "x", PAYLOAD, screen_name="acct")
    await run_worker(cfg, maker, transport=_ok())
    assert list(for_io(cfg.output_root).rglob("*.part")) == []
