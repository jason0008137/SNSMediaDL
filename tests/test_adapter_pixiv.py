"""pixiv adapter：id 清單列舉、多頁推導、動圖、增量、憑證。全部走 MockTransport。

⚠️ **這裡的 fixture 是手寫的，不是真實回應。**
它們反映的是「PixivBatchDownloader 的原始碼說 pixiv 長這樣」，沒有一項在
pixiv 本站驗證過。等自己打過一次真實 API 之後，要拿真實回應（塗掉憑證）
回來換掉這些。

在那之前，這些測試證明的是「程式碼照我們相信的形狀運作」，
**不是**「程式碼能對付真的 pixiv」。
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from snsmediadl.adapters import get_adapter
from snsmediadl.adapters.pixiv import (
    PixivAdapter,
    PixivFieldError,
    derive_page_url,
)
from snsmediadl.db.enums import MediaKind
from snsmediadl.services.fetch import fetch_account

ORIGINAL_P0 = "https://i.pximg.net/img-original/img/2026/01/02/03/04/05/111_p0.jpg"


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture()
def cfg_pixiv(cfg):
    # platform_credentials 是**假值**。真的憑證絕不進測試 fixture。
    return dataclasses.replace(
        cfg, platform_credentials={"pixiv": "fake-session-for-tests"}
    )


@pytest.fixture()
def fast_adapter(monkeypatch):
    """把註冊表裡的 pixiv adapter 節流關掉。

    正式的 1.8 秒間隔是刻意的（PBD 的 slowCrawlDealy），
    但測試不該真的等 —— 所以這裡改的是**測試環境**，不是預設值。
    刻意不做成 config 選項：間隔是平台屬性，開放調低等於邀請自己被鎖。
    """
    adapter = get_adapter("pixiv")
    monkeypatch.setattr(adapter, "detail_delay", 0.0)
    return adapter


def _illust(work_id: str, *, page_count: int = 1, x_restrict: int = 0,
            illust_type: int = 0) -> dict:
    return {
        "error": False,
        "body": {
            "id": work_id,
            "userId": "9999",
            "illustType": illust_type,
            "pageCount": page_count,
            "xRestrict": x_restrict,
            "createDate": "2026-01-02T03:04:05+09:00",
            "urls": {"original": ORIGINAL_P0.replace("111", work_id)},
        },
    }


def _server(work_ids: list[str], *, illusts: dict[str, dict] | None = None,
            pages_reply: dict | None = None):
    """假的 pixiv。回傳 (transport, 呼叫記錄)。"""
    calls: dict[str, int] = {"detail": 0, "profile_all": 0, "ugoira": 0, "pages": 0}
    illusts = illusts or {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path.endswith("/profile/all"):
            calls["profile_all"] += 1
            return httpx.Response(
                200,
                json={"error": False, "body": {
                    "illusts": {wid: None for wid in work_ids},
                    # 一件漫畫都沒有時 pixiv 回空陣列而不是空字典
                    "manga": [],
                }},
            )

        if path.endswith("/ugoira_meta"):
            calls["ugoira"] += 1
            work_id = path.split("/")[-2]
            return httpx.Response(200, json={"error": False, "body": {
                "originalSrc": f"https://i.pximg.net/img-zip/ugoira/{work_id}.zip",
                "mime_type": "image/jpeg",
                "frames": [{"file": "000000.jpg", "delay": 70}],
            }})

        if path.endswith("/pages"):
            calls["pages"] += 1
            return httpx.Response(200, json=pages_reply or {"error": False, "body": []})

        if path.startswith("/ajax/illust/"):
            calls["detail"] += 1
            work_id = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=illusts.get(work_id, _illust(work_id)))

        if path.startswith("/ajax/user/"):
            return httpx.Response(200, json={"error": False, "body": {
                "userId": "9999", "name": "テスト作者",
            }})

        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


# ── 純函式 ───────────────────────────────────────────────


def test_derive_page_url_replaces_only_the_suffix():
    assert derive_page_url(ORIGINAL_P0, 0) == ORIGINAL_P0
    assert derive_page_url(ORIGINAL_P0, 3).endswith("111_p3.jpg")
    # 路徑中間的數字不該被誤傷
    assert "/2026/01/02/" in derive_page_url(ORIGINAL_P0, 3)


def test_derive_page_url_raises_instead_of_guessing():
    """推導不出來要炸，不可以回一個「可能對」的網址。

    回錯的網址會把「pximg 換了 URL 樣式」變成幾百個看不出原因的 404。
    """
    with pytest.raises(PixivFieldError, match="無法從"):
        derive_page_url("https://i.pximg.net/img-original/no-page-marker.jpg", 1)


def test_download_headers_carry_referer_but_never_credentials():
    headers = PixivAdapter().download_headers("https://i.pximg.net/x.jpg")
    assert headers["Referer"] == "https://www.pixiv.net/"
    # CDN 不需要憑證 —— 憑證只活在列舉階段
    assert "Cookie" not in headers


def test_auth_headers_refuses_to_run_without_credentials(cfg):
    with pytest.raises(PixivFieldError, match="platform_credentials"):
        PixivAdapter().auth_headers(cfg, "")


def test_auth_headers_use_php_session(cfg_pixiv):
    headers = PixivAdapter().auth_headers(cfg_pixiv, "")
    assert headers["Cookie"] == "PHPSESSID=fake-session-for-tests"


def test_estimate_seconds_counts_gaps_not_requests():
    adapter = PixivAdapter(detail_delay=1.8)
    assert adapter.estimate_seconds(0) == 0.0
    assert adapter.estimate_seconds(1) == 0.0  # 第一個不等待
    assert adapter.estimate_seconds(101) == pytest.approx(180.0)


async def test_resolve_account_rejects_non_numeric_handle(cfg_pixiv):
    transport, _ = _server([])
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PixivFieldError, match="數字 user id"):
            await PixivAdapter().resolve_account(client, "", "@someartist")


# ── 列舉 ─────────────────────────────────────────────────


async def test_list_work_ids_merges_sections_newest_first(cfg_pixiv):
    transport, _ = _server(["100", "300", "200"])
    adapter = PixivAdapter(detail_delay=0)
    async with httpx.AsyncClient(transport=transport) as client:
        account = await adapter.resolve_account(client, "", "9999")
        ids = await adapter.list_work_ids(client, account)
    assert ids == ["300", "200", "100"]


async def test_list_work_ids_raises_when_section_missing(cfg_pixiv):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/profile/all"):
            # manga 不見了 = pixiv 改版。要炸，不可以當成「沒有漫畫」。
            return httpx.Response(200, json={"error": False, "body": {"illusts": {}}})
        return httpx.Response(200, json={"error": False, "body": {
            "userId": "9999", "name": "n"}})

    adapter = PixivAdapter(detail_delay=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        account = await adapter.resolve_account(client, "", "9999")
        with pytest.raises(PixivFieldError, match="manga"):
            await adapter.list_work_ids(client, account)


async def test_pixiv_error_body_is_not_treated_as_data(cfg_pixiv):
    """pixiv 的錯誤是 HTTP 200 + error:true，raise_for_status 抓不到。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": True, "message": "該当作品は削除されました"})

    adapter = PixivAdapter(detail_delay=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PixivFieldError, match="回報錯誤"):
            await adapter.resolve_account(client, "", "9999")


# ── 端對端（走 fetch_account）────────────────────────────


async def test_multipage_work_expands_to_one_media_per_page(
    cfg_pixiv, maker, fast_adapter
):
    transport, calls = _server(["111"], illusts={"111": _illust("111", page_count=3)})

    r = await fetch_account(cfg_pixiv, maker, platform="pixiv", host="",
                            acct="9999", transport=transport)

    assert r.works_total == 1
    assert r.posts_new == 1
    assert r.media_new == 3
    # 關鍵：3 頁只花 1 個詳情請求，沒有打 /pages
    assert calls["detail"] == 1
    assert calls["pages"] == 0

    with maker() as s:
        from snsmediadl.db.models import Media

        rows = s.query(Media).order_by(Media.ordinal).all()
        assert [m.ordinal for m in rows] == [0, 1, 2]
        assert [m.platform_media_key for m in rows] == ["111_p0", "111_p1", "111_p2"]
        assert rows[2].source_url.endswith("111_p2.jpg")
        assert all(m.kind == MediaKind.PHOTO.value for m in rows)


async def test_ugoira_is_stored_as_zip_with_frame_data(cfg_pixiv, maker, fast_adapter):
    transport, calls = _server(
        ["222"], illusts={"222": _illust("222", illust_type=2)}
    )

    r = await fetch_account(cfg_pixiv, maker, platform="pixiv", host="",
                            acct="9999", transport=transport)

    assert r.media_new == 1
    assert calls["ugoira"] == 1

    with maker() as s:
        from snsmediadl.db.models import Media

        media = s.query(Media).one()
        assert media.kind == MediaKind.UGOIRA.value
        assert media.source_url.endswith(".zip")
        assert media.platform_media_key == "222_ugoira"
        # 幀延遲要留著：之後要轉檔時不必重抓
        assert "delay" in (media.meta_json or "")


async def test_x_restrict_becomes_hint_not_authority(cfg_pixiv, maker, fast_adapter):
    transport, _ = _server(["333"], illusts={"333": _illust("333", x_restrict=1)})

    await fetch_account(cfg_pixiv, maker, platform="pixiv", host="",
                        acct="9999", transport=transport)

    with maker() as s:
        from snsmediadl.db.models import Post

        post = s.query(Post).one()
        # auto 猜測，不是人工標記
        assert post.rating == "r18"
        assert post.rating_source == "auto"


async def test_incremental_makes_zero_detail_requests_on_rerun(
    cfg_pixiv, maker, fast_adapter
):
    """pixiv 增量的重點不是「不重複入庫」，是**根本不發詳情請求**。

    id 清單一次到手，跟 DB 一比就知道要問誰 —— 在 1.8 秒一個請求的節流下，
    這個順序差的是幾十分鐘。
    """
    transport, calls = _server(["101", "102"])

    first = await fetch_account(cfg_pixiv, maker, platform="pixiv", host="",
                                acct="9999", transport=transport)
    assert first.posts_new == 2
    assert calls["detail"] == 2

    second = await fetch_account(cfg_pixiv, maker, platform="pixiv", host="",
                                 acct="9999", transport=transport)
    assert second.works_total == 2
    assert second.works_to_fetch == 0
    assert second.posts_new == 0
    assert second.stopped_because == "全部作品都抓過了（增量）"
    # 沒有多打任何一個詳情請求
    assert calls["detail"] == 2


async def test_full_ignores_incremental_filter(cfg_pixiv, maker, fast_adapter):
    transport, calls = _server(["101"])

    await fetch_account(cfg_pixiv, maker, platform="pixiv", host="",
                        acct="9999", transport=transport)
    await fetch_account(cfg_pixiv, maker, platform="pixiv", host="",
                        acct="9999", full=True, transport=transport)

    # full 會重問，但 ingest 仍然去重
    assert calls["detail"] == 2
    with maker() as s:
        from snsmediadl.db.models import Post

        assert s.query(Post).count() == 1
