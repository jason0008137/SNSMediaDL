"""下載節奏是**平台屬性**，不是全域設定。

⚠️ 這支測試存在的理由是實際踩過的坑（2026-08-20）：
`Config.download_delay_seconds` 預設 1 秒，套在**所有**平台上，而它的理由
從頭到尾都是 X 的（超速會鎖整個帳號約一天）。後果很具體：

  · `concurrency` 設 4，實際吞吐仍然是每秒 1 個檔 —— semaphore 完全失效
  · pixiv 明明沒有這個限制，卻跟著 X 一起被綁住
  · 更糟的是 Throttle 是**全域單一實例**：pixiv 的下載要排隊等 X，
    而兩者打的是完全不同的 host

求證來源（`RefRepo/PixivBatchDownloader`）：它的慢速抓取
（`slowCrawlDealy`，預設 1800ms、下限 1000ms、只在 >100 件時啟用）**只包住
`getWorksData()`**，也就是作品資料 API。下載端是 `downloadThreadMax = 6`
條並行，任何一條成功或失敗都**立刻** `createDownload(no)` 補回同一個槽位，
中間沒有延遲；下載路徑上僅有的 `sleep` 是錯誤重試退避。

也就是說：限速在列舉那一段，媒體 CDN 沒有。
"""

from __future__ import annotations

import dataclasses
import time

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from snsmediadl.adapters import get_adapter
from snsmediadl.db.models import Account, Media, Post
from snsmediadl.downloader import run_worker
from snsmediadl.downloader.worker import _platform_delay
from snsmediadl.services.ingest import ingest


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def _payload(n: int, tag: str) -> list[dict]:
    return [{
        "postId": f"{tag}{i}", "userId": f"u-{tag}",
        "createdAt": "Tue Jul 08 11:43:52 +0000 2025",
        "media": [{"kind": "photo", "url": f"https://{tag}/{i}.jpg",
                   "orig": f"https://{tag}/{i}.jpg?name=orig"}],
    } for i in range(n)]


def _ok():
    return httpx.MockTransport(lambda req: httpx.Response(200, content=b"x"))


def _seed_pixiv(session, n: int) -> None:
    """直接建列。

    ⚠️ pixiv **不走 `ingest()`** —— 它的資料是 backend 自己抓的
    （`POST /api/fetch`），`PixivAdapter.normalize()` 會直接丟
    NotImplementedError。這裡要的只是「佇列裡有幾筆 pixiv 待下載」，
    所以繞過採集那一層直接寫進 DB。
    """
    acct = Account(platform="pixiv", platform_user_id="u-px", screen_name="acct-px")
    session.add(acct)
    session.flush()
    for i in range(n):
        post = Post(platform="pixiv", platform_post_id=f"px{i}", account_id=acct.id)
        session.add(post)
        session.flush()
        session.add(Media(
            post_id=post.id, ordinal=0, kind="photo",
            source_url=f"https://i.pximg.net/{i}.jpg", status="pending",
        ))
    session.commit()


def test_pixiv_does_not_inherit_the_x_download_delay(cfg) -> None:
    """pixiv 的下載間隔是 0，X 是 1 秒。**兩者不可以相等。**

    相等就代表節奏又被拉回全域設定了 —— 那正是這一輪要修掉的東西。
    """
    base = dataclasses.replace(cfg, download_delay_seconds=None)
    assert _platform_delay(base, "pixiv") == 0.0, (
        "pixiv 又被套上下載間隔了。它的限速在列舉那一段，不在媒體 CDN"
        "（證據見本檔 docstring 與 adapters/pixiv.py 的註解）。"
    )
    assert _platform_delay(base, "x") == 1.0, (
        "X 的下載間隔不見了。X 超速會**鎖整個帳號約一天** —— "
        "這個 1 秒不是效能取捨，是帳號安全。"
    )


def test_delay_lives_on_the_adapter_not_the_config() -> None:
    """每個 adapter 都要自己講出它的下載間隔。

    漏了的症狀是新平台默默沿用別人的政策 —— 太快會被封，太慢是白等，
    而兩者都不會有錯誤訊息。
    """
    for platform in ("x", "pixiv", "misskey", "mastodon"):
        policy = get_adapter(platform).rate_limit_policy
        assert policy.download_delay_seconds >= 0.0, platform


def test_config_override_replaces_the_platform_value(cfg) -> None:
    """設了數值就**覆寫**平台值，不是取下限。

    ⚠️ 一開始寫成 `max(平台值, 設定值)`，於是 `conftest` 設的 0.0 失效：
    測試回頭去等 X 的 1 秒，`test_run_writes_real_files` 的 5 秒逾時被
    6 個檔 × 1 秒撐爆。而且那樣的話使用者再也沒辦法把節流關掉。
    """
    off = dataclasses.replace(cfg, download_delay_seconds=0.0)
    assert _platform_delay(off, "x") == 0.0, "覆寫成 0 應該要能把 X 的節流關掉"

    slow = dataclasses.replace(cfg, download_delay_seconds=3.0)
    assert _platform_delay(slow, "pixiv") == 3.0, "覆寫應該也能把 pixiv 調慢"


def test_none_means_use_the_platform_value(cfg) -> None:
    """None 是預設，代表「各平台照自己的來」。"""
    base = dataclasses.replace(cfg, download_delay_seconds=None)
    assert _platform_delay(base, "pixiv") != _platform_delay(base, "x")


def test_unknown_platform_raises_instead_of_guessing(cfg) -> None:
    """認不得的平台要炸，不要默默給一個保守值。

    默默給值會讓「新平台忘了註冊 adapter」變成一個只是「跑得有點慢」的
    症狀 —— 沒有錯誤訊息，而且幾個月後才會有人覺得奇怪。
    """
    base = dataclasses.replace(cfg, download_delay_seconds=None)
    with pytest.raises(Exception):
        _platform_delay(base, "not_a_real_platform")


async def test_pixiv_does_not_queue_behind_x(cfg, session, maker) -> None:
    """pixiv 的下載**不排在 X 後面**。

    這是整個改動的行為主張，而它有一個很乾淨的判別式：3 個 X + 3 個 pixiv，
    X 的間隔 1 秒、pixiv 0 秒。

      · 共用一個全域 Throttle → 6 次開始都要排隊 → 至少 5 秒
      · 每個平台各一個        → 只有 X 排隊（3 次開始 = 2 段間隔）→ 約 2 秒

    兩者差得夠遠，不會因為機器快慢而誤判。

    ⚠️ 用 `download_delay_seconds=None`（預設）才測得到平台值 ——
    填任何數字都會覆寫掉兩邊，這條測試就變成永遠會過的空測試。
    """
    base = dataclasses.replace(cfg, download_delay_seconds=None, concurrency=5)
    ingest(session, "x", _payload(3, "x"), screen_name="acct-x")
    _seed_pixiv(session, 3)

    start = time.perf_counter()
    stats = await run_worker(base, maker, transport=_ok())
    elapsed = time.perf_counter() - start

    assert stats.done == 6
    assert elapsed >= 1.8, (
        f"只花了 {elapsed:.2f}s —— X 的節流不見了。"
        "X 超速會鎖整個帳號約一天，那個 1 秒不是效能取捨。"
    )
    assert elapsed < 4.0, (
        f"花了 {elapsed:.2f}s，接近共用節流的 5 秒 —— "
        "pixiv 又在排隊等 X 了。兩者打的是完全不同的 host。"
    )
