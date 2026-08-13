"""媒體檔案端點。這個端點會把磁碟檔案吐出去，路徑防護不是選配。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Media
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
