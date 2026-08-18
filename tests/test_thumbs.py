"""格線縮圖端點。

存在的理由是實測數字：正式庫 224 萬個媒體、總計 1.27 TB、單檔最大 446 MB，
而格線一格只顯示 160px 見方。原本直接吐原檔 = 一頁上百 MB。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from snsmediadl.api.app import create_app, get_session
from snsmediadl.api.files import IMMUTABLE, THUMB_MAX_EDGE
from snsmediadl.db.enums import MediaStatus
from snsmediadl.db.models import Account, Media, Post


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _make_media(session, cfg, name: str, size=(1200, 800), mode="RGB",
                fmt="JPEG", make_file=True) -> int:
    """建一筆 media 記錄，並在 output_root 底下放一個真的檔案。"""
    acct = Account(platform="x", platform_user_id=f"u_{name}", screen_name=name)
    session.add(acct)
    session.flush()
    post = Post(platform="x", platform_post_id=f"p_{name}", account_id=acct.id)
    session.add(post)
    session.flush()

    path = cfg.output_root / name
    if make_file:
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "RAW":            # 不是圖片，用來測影片那條路
            path.write_bytes(b"not an image at all")
        else:
            Image.new(mode, size, "red").save(path, fmt)

    m = Media(post_id=post.id, ordinal=0, kind="photo",
              source_url="https://example.invalid/x", local_path=str(path),
              status=MediaStatus.DONE.value)
    session.add(m)
    session.commit()
    return m.id


# ── 正常路徑 ──────────────────────────────────────────────

def test_thumb_is_shrunk_and_webp(client, session, cfg):
    mid = _make_media(session, cfg, "big.jpg", size=(1200, 800))

    r = client.get(f"/api/media/{mid}/thumb")

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    import io
    with Image.open(io.BytesIO(r.content)) as im:
        assert max(im.size) == THUMB_MAX_EDGE
        assert im.size == (320, 213)      # 長寬比要保留，不可拉伸


def test_thumb_is_much_smaller_than_original(client, session, cfg):
    """這支端點存在的**唯一**理由。縮圖沒有比較小就等於白做。"""
    mid = _make_media(session, cfg, "photo.png", size=(2000, 2000), fmt="PNG")

    original = client.get(f"/api/media/{mid}/file").content
    thumb = client.get(f"/api/media/{mid}/thumb").content

    assert len(thumb) < len(original) / 10


def test_thumb_is_cached_on_disk(client, session, cfg):
    mid = _make_media(session, cfg, "cached.jpg")
    client.get(f"/api/media/{mid}/thumb")

    cached = list(cfg.thumb_dir.rglob("*.webp"))
    assert len(cached) == 1
    assert cached[0].name == f"{mid}.webp"
    # 分桶：不可以全部堆在 thumb_dir 根目錄（正式庫有 224 萬個）
    assert cached[0].parent != cfg.thumb_dir


def test_second_request_reuses_cache_not_the_original(client, session, cfg):
    """快取命中時**不可以再開原檔** —— 原檔可能在沒插的碟上。"""
    mid = _make_media(session, cfg, "reuse.jpg")
    first = client.get(f"/api/media/{mid}/thumb").content

    # 把原檔換成壞資料。仍讀得到縮圖 = 真的走了快取。
    (cfg.output_root / "reuse.jpg").write_bytes(b"corrupted now")

    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 200
    assert r.content == first


def test_no_part_files_left_behind(client, session, cfg):
    """先寫 .part 再 rename。收工後不該留下暫存檔。"""
    mid = _make_media(session, cfg, "atomic.jpg")
    client.get(f"/api/media/{mid}/thumb")
    assert not list(cfg.thumb_dir.rglob("*.part"))


def test_palette_and_alpha_images_survive(client, session, cfg):
    """P 模式（GIF 調色盤）直接存 WebP 會失敗或掉色。"""
    p_mode = _make_media(session, cfg, "pal.gif", mode="P", fmt="GIF")
    alpha = _make_media(session, cfg, "alpha.png", mode="RGBA", fmt="PNG")

    assert client.get(f"/api/media/{p_mode}/thumb").status_code == 200
    assert client.get(f"/api/media/{alpha}/thumb").status_code == 200


# ── 快取標頭 ──────────────────────────────────────────────

def test_both_endpoints_set_immutable_cache_header(client, session, cfg):
    """少了這個，捲回上一頁就是整頁重傳。"""
    mid = _make_media(session, cfg, "hdr.jpg")
    assert client.get(f"/api/media/{mid}/thumb").headers["cache-control"] == IMMUTABLE
    assert client.get(f"/api/media/{mid}/file").headers["cache-control"] == IMMUTABLE


# ── 失敗路徑：每一種都要能被分辨 ─────────────────────────

def test_unsupported_format_returns_415_not_404(client, session, cfg):
    """415 而不是 404 —— 前端要能分辨「這種格式沒救」與「檔案不見了」。

    ⚠️ 影片**不再**走這條路（它現在有縮圖，見 test_video_thumbs.py）。
    415 現在只留給真正不支援的格式。
    """
    mid = _make_media(session, cfg, "weird.psd", fmt="RAW")
    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 415


def test_missing_file_returns_404(client, session, cfg):
    mid = _make_media(session, cfg, "gone.jpg", make_file=False)
    assert client.get(f"/api/media/{mid}/thumb").status_code == 404


def test_unknown_media_returns_404(client):
    assert client.get("/api/media/999999/thumb").status_code == 404


def test_not_downloaded_returns_409(client, session, cfg):
    acct = Account(platform="x", platform_user_id="u", screen_name="u")
    session.add(acct)
    session.flush()
    post = Post(platform="x", platform_post_id="p", account_id=acct.id)
    session.add(post)
    session.flush()
    m = Media(post_id=post.id, ordinal=0, kind="photo",
              source_url="https://example.invalid/x", local_path=None,
              status=MediaStatus.PENDING.value)
    session.add(m)
    session.commit()

    assert client.get(f"/api/media/{m.id}/thumb").status_code == 409


def test_corrupt_image_returns_500_not_a_placeholder(client, session, cfg):
    """⚠️ 壞檔**不可以**回一張灰色佔位圖。

    那等於把壞檔藏起來 —— 使用者會以為那張圖本來就長那樣，
    幾個月後才發現一整批是壞的。
    """
    mid = _make_media(session, cfg, "broken.jpg", fmt="RAW")
    # .jpg 副檔名在白名單裡，所以會真的走進 Pillow 然後失敗
    r = client.get(f"/api/media/{mid}/thumb")
    assert r.status_code == 500
    assert not list(cfg.thumb_dir.rglob("*.webp"))     # 不留下半成品


def test_path_outside_media_roots_is_rejected(client, session, cfg, tmp_path):
    """路徑白名單對縮圖同樣有效 —— 這支也會開磁碟上的檔案。"""
    outside = tmp_path / "outside.jpg"
    Image.new("RGB", (10, 10)).save(outside, "JPEG")

    acct = Account(platform="x", platform_user_id="u2", screen_name="u2")
    session.add(acct)
    session.flush()
    post = Post(platform="x", platform_post_id="p2", account_id=acct.id)
    session.add(post)
    session.flush()
    m = Media(post_id=post.id, ordinal=0, kind="photo",
              source_url="https://example.invalid/x", local_path=str(outside),
              status=MediaStatus.DONE.value)
    session.add(m)
    session.commit()

    assert client.get(f"/api/media/{m.id}/thumb").status_code == 403


# ── 設定 ──────────────────────────────────────────────────

def test_thumb_dir_defaults_under_output_root(cfg):
    """使用者拍板：`<output_root>/thumb`。"""
    assert cfg.thumb_dir == cfg.output_root / "thumb"


def test_thumb_root_can_be_overridden(cfg, tmp_path):
    cfg.thumb_root = tmp_path / "elsewhere"
    assert cfg.thumb_dir == tmp_path / "elsewhere"
