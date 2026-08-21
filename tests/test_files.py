"""媒體檔案端點。這個端點會把磁碟檔案吐出去，路徑防護不是選配。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Media
from snsmediadl.fspath import for_io
from snsmediadl.services.ingest import ingest

PAYLOAD = [{
    "postId": "p1", "userId": "u1", "createdAt": "Tue Jul 08 11:43:52 +0000 2025",
    "media": [{"kind": "photo", "url": "https://x/a.jpg", "orig": "https://x/a.jpg?name=orig"}],
}]


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


@pytest.fixture()
def media(cfg, session):
    ingest(session, "x", PAYLOAD, screen_name="acct")
    m = session.scalar(select(Media))
    target = cfg.output_root / "x" / "acct" / "a.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff-fake-jpeg")
    m.local_path = str(target)
    m.status = "done"
    session.commit()
    return m


def test_serves_downloaded_file(client, media):
    r = client.get(f"/api/media/{media.id}/file")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff-fake-jpeg"
    assert r.headers["content-type"].startswith("image/")


def test_head_matches_get(client, session, media):
    """HEAD 必須與 GET 給同一個答案。

    ⚠️ 這不是潔癖，是 GUI 的診斷來源：`<img>` / `<video>` 的 error 事件拿不到
    狀態碼，所以前端補一次 HEAD 去問「為什麼讀不到」（404 檔案不在 /
    415 格式生不出縮圖 / 500 原檔壞了）。

    而 FastAPI 的 `@router.get` **只註冊 GET**（與 Starlette 的裸 Route 不同）——
    HEAD 會一路掉到掛在 "/" 的靜態檔 mount 回 404。症狀不是壞掉，是
    **每一種失敗都被說成「檔案被刪除，或那顆碟沒插」**，也就是捏造診斷。
    """
    for path in (f"/api/media/{media.id}/file", f"/api/media/{media.id}/thumb"):
        head = client.head(path)
        get = client.get(path)
        assert head.status_code == get.status_code, path
        assert head.content == b"", "HEAD 不該有 body"

    # 檔案不在時，HEAD 也要說 404（而不是靜態 mount 的那個 404 ——
    # 兩者狀態碼一樣，所以這裡多驗一次 content-type 確認是我們回的）
    session.get(type(media), media.id).local_path = str(
        media.local_path) + ".gone"
    session.commit()
    r = client.head(f"/api/media/{media.id}/file")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_path_traversal_is_rejected(client, session, media, tmp_path):
    """local_path 目前是自己寫的，但一旦被污染就是任意檔案讀取。"""
    outside = tmp_path / "secret.txt"
    outside.write_text("credentials", encoding="utf-8")

    media.local_path = str(outside)
    session.commit()

    r = client.get(f"/api/media/{media.id}/file")
    assert r.status_code == 403
    assert "credentials" not in r.text


def test_relative_traversal_is_rejected(client, session, media, cfg):
    media.local_path = str(cfg.output_root / ".." / ".." / "windows" / "win.ini")
    session.commit()
    assert client.get(f"/api/media/{media.id}/file").status_code == 403


def test_sibling_prefix_directory_is_rejected(client, session, media, cfg):
    """字串前綴比對會被 <root>-evil 這種騙過，所以用 is_relative_to。"""
    evil = cfg.output_root.parent / (cfg.output_root.name + "-evil")
    evil.mkdir(parents=True, exist_ok=True)
    target = evil / "x.jpg"
    target.write_bytes(b"nope")

    media.local_path = str(target)
    session.commit()
    assert client.get(f"/api/media/{media.id}/file").status_code == 403


def test_missing_file_is_404_not_500(client, session, media):
    """檔案被手動刪掉是常見情況，GUI 要能標示而不是整頁壞掉。"""
    from pathlib import Path
    Path(media.local_path).unlink()
    assert client.get(f"/api/media/{media.id}/file").status_code == 404


def test_not_downloaded_yet_is_409(client, session):
    ingest(session, "x", PAYLOAD, screen_name="acct")
    m = session.scalar(select(Media))
    assert client.get(f"/api/media/{m.id}/file").status_code == 409


def test_unknown_media_is_404(client):
    assert client.get("/api/media/9999/file").status_code == 404


# ── 多個媒體根目錄 ────────────────────────────────────────
#
# 換下載目錄之後舊檔要還看得到。這是 extra_media_roots 存在的唯一理由，
# 沒有這幾個測試，功能壞掉不會有人發現 —— 舊檔只是靜靜地變成 403。

def test_file_in_old_root_is_served(cfg, session, media, tmp_path):
    """改了 output_root 之後，舊根目錄底下的檔案仍要提供。"""
    old_root = tmp_path / "old_downloads"
    old_file = old_root / "x" / "acct" / "old.jpg"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_bytes(b"\xff\xd8\xff-old")

    media.local_path = str(old_file)
    session.commit()

    cfg.output_root = tmp_path / "new_downloads"
    cfg.extra_media_roots = [old_root]
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session

    r = TestClient(app).get(f"/api/media/{media.id}/file")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8\xff-old"


def test_file_outside_every_root_is_still_rejected(cfg, session, media, tmp_path):
    """多根目錄不能變成「什麼都給」—— 白名單以外照樣 403。"""
    secret = tmp_path / "secret.txt"
    secret.write_text("credentials", encoding="utf-8")

    media.local_path = str(secret)
    session.commit()

    cfg.extra_media_roots = [tmp_path / "archive"]
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session

    r = TestClient(app).get(f"/api/media/{media.id}/file")
    assert r.status_code == 403
    assert "credentials" not in r.text


def test_unmounted_root_does_not_break_the_others(cfg, session, media, tmp_path):
    """外接硬碟沒插是常態：不存在的根目錄要跳過，不能讓其他根目錄一起壞掉。"""
    cfg.extra_media_roots = [tmp_path / "no_such_drive" / "media"]
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session

    # media 這筆落在 cfg.output_root 底下，仍應正常提供
    assert TestClient(app).get(f"/api/media/{media.id}/file").status_code == 200


# ── 長路徑（MAX_PATH）─────────────────────────────────
#
# 正式庫實測（2026-08-21）：606 筆 `local_path` 超過 260 字元，全部被回
# `file.missing` 404 加上「被刪掉，或那顆碟沒插」—— 而那 606 個檔案一個都沒少。
# 根因是 Windows 的 MAX_PATH，修法見 `snsmediadl/fspath.py`。
# 這一節守的就是「別再退化回去」。

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="MAX_PATH 是 Windows 的事")


def _deep_media(cfg, session, media, *, content: bytes = b"\xff\xd8\xff-deep") -> None:
    """把 `media` 的檔案搬到一條 >260 字元的路徑上。"""
    deep = cfg.output_root
    while len(str(deep)) < 250:
        deep = deep / ("d" * 40)
    target = deep / ("n" * 30 + ".jpg")
    assert len(str(target)) > 260, f"沒墊夠長：{len(str(target))}"

    for_io(deep).mkdir(parents=True, exist_ok=True)
    for_io(target).write_bytes(content)
    # ⚠️ 存進 DB 的是**一般路徑**，跟匯入器寫進去的形狀一致。
    #    測試若存 `\?\` 形狀就等於在驗一件正式庫不會發生的事。
    media.local_path = str(target)
    session.commit()


@WINDOWS_ONLY
def test_the_fixture_really_is_unreachable_without_the_prefix(cfg, session, media):
    """先證明這個測試驗得到東西 —— 沒有前綴時 Python 說這個檔不存在。"""
    _deep_media(cfg, session, media)
    assert not Path(media.local_path).exists()
    assert for_io(media.local_path).exists()


@WINDOWS_ONLY
def test_long_path_file_is_served(client, cfg, session, media):
    _deep_media(cfg, session, media)
    r = client.get(f"/api/media/{media.id}/file")
    assert r.status_code == 200, r.text
    assert r.content == b"\xff\xd8\xff-deep"


@WINDOWS_ONLY
def test_long_path_head_is_not_a_404(client, cfg, session, media):
    """GUI 靠 HEAD 判斷「為什麼讀不到」。長路徑不可以被說成「檔案不見了」。"""
    _deep_media(cfg, session, media)
    assert client.head(f"/api/media/{media.id}/file").status_code == 200


@WINDOWS_ONLY
def test_long_path_thumb_is_generated(client, cfg, session, media):
    """縮圖走的是另一條路（Pillow 開原檔），要各自驗過。"""
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (600, 400), "red").save(buf, "JPEG")
    _deep_media(cfg, session, media, content=buf.getvalue())

    r = client.get(f"/api/media/{media.id}/thumb")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/webp"


@WINDOWS_ONLY
def test_long_path_outside_root_is_still_403(cfg, session, media, tmp_path):
    """長路徑不是繞過白名單的後門：檢查仍用一般路徑做，根目錄外照樣拒絕。"""
    outside = tmp_path / "elsewhere"
    deep = outside
    while len(str(deep)) < 250:
        deep = deep / ("d" * 40)
    target = deep / ("n" * 30 + ".jpg")
    for_io(deep).mkdir(parents=True, exist_ok=True)
    for_io(target).write_bytes(b"secret")

    media.local_path = str(target)
    session.commit()

    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    r = TestClient(app).get(f"/api/media/{media.id}/file")
    assert r.status_code == 403
    assert "secret" not in r.text


@WINDOWS_ONLY
def test_missing_long_path_is_still_a_real_404(client, cfg, session, media):
    """修好之後，這個 404 才真的代表「檔案不在了」—— 那個訊息不可以再說謊。"""
    _deep_media(cfg, session, media)
    for_io(media.local_path).unlink()

    r = client.get(f"/api/media/{media.id}/file")
    assert r.status_code == 404
    assert r.json()["code"] == "file.missing"
