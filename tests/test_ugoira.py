"""ugoira 逐格供應。

pixiv 的動圖是一包 zip 裝一堆 jpg。這裡驗的是「拆包給前端」這條路，
以及**每一種拆不開的情況都要說得清清楚楚**——因為靜默兜過去的代價是
動畫用錯的速度播、或少了幾格，而畫面上不會有任何跡象。
"""

from __future__ import annotations

import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session
from snsmediadl.db.models import Account, Media, Post

# 三格，延遲刻意不等長——等長的表會讓「累積時間」的錯誤實作也剛好通過。
FRAMES = [
    {"file": "000000.jpg", "delay": 40},
    {"file": "000001.jpg", "delay": 70},
    {"file": "000002.jpg", "delay": 120},
]
BODIES = {
    "000000.jpg": b"\xff\xd8\xff-frame-zero",
    "000001.jpg": b"\xff\xd8\xff-frame-one",
    "000002.jpg": b"\xff\xd8\xff-frame-two",
}


def write_zip(path, names=None, *, compression=zipfile.ZIP_STORED):
    """寫一包 ugoira zip。

    ⚠️ 預設 **`ZIP_STORED`（不壓縮）**，因為真的 pixiv 檔就是這樣——
    2026-08-22 對實檔驗證：96 格、8.6 MB、`compress_type == 0`。
    用 DEFLATE 寫測試資料會讓測試偏離它要保護的那個形狀。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression) as zf:
        for name in (names if names is not None else list(BODIES)):
            zf.writestr(name, BODIES.get(name, b"\xff\xd8\xff-extra"))
    return path


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def make_media(cfg, session, *, kind="ugoira", meta=..., names=None, zip_ok=True):
    acct = Account(platform="pixiv", platform_user_id="u1", screen_name="acct")
    session.add(acct)
    session.flush()
    post = Post(platform="pixiv", platform_post_id="p1", account_id=acct.id)
    session.add(post)
    session.flush()

    target = cfg.output_root / "pixiv" / "acct" / "148693764_0.zip"
    if zip_ok:
        write_zip(target, names)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not a zip at all")

    if meta is ...:
        meta = {"illust_type": 2, "frames": FRAMES, "mime_type": "image/jpeg"}
    m = Media(
        post_id=post.id, ordinal=0, kind=kind, status="done",
        local_path=str(target),
        meta_json=json.dumps(meta) if meta is not None else None,
    )
    session.add(m)
    session.commit()
    return m


# ── 正常路徑 ──────────────────────────────────────────

def test_meta_lists_every_frame_delay(client, cfg, session):
    m = make_media(cfg, session)
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 200
    assert r.json() == {
        "count": 3,
        "total_ms": 230,
        "mime_type": "image/jpeg",
        "frames": [{"delay": 40}, {"delay": 70}, {"delay": 120}],
    }


def test_frame_bytes_are_the_original_bytes(client, cfg, session):
    """原檔直出。**不重新編碼**——手上有無損原檔，再壓一次只是損失。"""
    m = make_media(cfg, session)
    for i, name in enumerate(BODIES):
        r = client.get(f"/api/media/{m.id}/ugoira/{i}")
        assert r.status_code == 200, i
        assert r.content == BODIES[name], i
        assert r.headers["content-type"].startswith("image/")
        assert "immutable" in r.headers["cache-control"]


def test_head_matches_get_without_a_body(client, cfg, session):
    """HEAD 必須答同一個狀態碼，而且不帶 body。

    少了 HEAD，它會一路掉到掛在 "/" 的靜態檔 mount 回 404 ——
    也就是「這格不存在」，而真相是「這個方法沒註冊」。
    """
    m = make_media(cfg, session)
    for path in (f"/api/media/{m.id}/ugoira", f"/api/media/{m.id}/ugoira/1"):
        head = client.head(path)
        assert head.status_code == client.get(path).status_code, path
        assert head.content == b"", f"{path} 的 HEAD 不該有 body"


# ── 說得清楚的失敗 ────────────────────────────────────

def test_out_of_range_frame_is_404(client, cfg, session):
    m = make_media(cfg, session)
    for bad in (3, 99, -1):
        r = client.get(f"/api/media/{m.id}/ugoira/{bad}")
        assert r.status_code == 404, bad
        assert r.json()["code"] == "ugoira.frame_out_of_range"


def test_non_ugoira_is_415_not_404(client, cfg, session):
    """檔案在，只是它不是動圖——與「找不到」必須分得開。"""
    m = make_media(cfg, session, kind="photo")
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 415
    assert r.json()["code"] == "ugoira.not_ugoira"


def test_non_ugoira_says_so_even_when_the_file_is_gone(client, cfg, session):
    """⚠️ 「這不是動圖」必須在「讀不到檔案」**之前**回答。

    2026-08-22 實測踩到：對一筆 photo 問動圖資料，先撞上路徑檢查，於是畫面上
    寫「原檔不在了 —— 被刪掉，或那顆碟沒插」。那是**捏造診斷** ——
    檔案在不在跟「這個請求根本不合理」是兩件事，而使用者會照著錯的那句去查。
    """
    m = make_media(cfg, session, kind="photo")
    m.local_path = str(m.local_path) + ".gone"
    session.commit()
    for path in (f"/api/media/{m.id}/ugoira", f"/api/media/{m.id}/ugoira/0"):
        r = client.get(path)
        assert r.status_code == 415, path
        assert r.json()["code"] == "ugoira.not_ugoira", path


@pytest.mark.parametrize("meta", [None, {}, {"illust_type": 2}, {"frames": []}])
def test_missing_frame_data_refuses_instead_of_guessing(client, cfg, session, meta):
    """⚠️ 本專案的核心紅線：**不准猜一個 fps 頂替。**

    猜出來的動畫會用錯的速度播放，而畫面上沒有任何跡象告訴使用者它是錯的。
    正解是明說缺資料，讓人去重抓 `ugoira_meta`。
    """
    m = make_media(cfg, session, meta=meta)
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 409
    assert r.json()["code"] == "ugoira.no_frame_data"


def test_frame_list_and_zip_must_agree(client, cfg, session):
    """幀表說有三格，zip 裡只有兩格 → 報錯，**不取交集**。

    取交集的話 pixiv 改了打包方式時只會少幾格，沒有錯誤訊息 ——
    那是幾個月後才會有人發現的那種 bug。
    """
    m = make_media(cfg, session, names=["000000.jpg", "000001.jpg"])
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 500
    assert r.json()["code"] == "ugoira.frame_mismatch"


def test_extra_entries_in_zip_also_mismatch(client, cfg, session):
    """多出來的檔案同樣是不一致——zip 裡有東西是幀表不知道的。"""
    m = make_media(cfg, session, names=[*BODIES, "000003.jpg"])
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 500
    assert r.json()["code"] == "ugoira.frame_mismatch"


def test_single_frame_missing_from_zip_is_reported(client, cfg, session):
    """逐格端點不重掃全表（那要每格付一次），但對不上時要回同一個 code。"""
    m = make_media(cfg, session, names=["000000.jpg", "000002.jpg"])
    r = client.get(f"/api/media/{m.id}/ugoira/1")
    assert r.status_code == 500
    assert r.json()["code"] == "ugoira.frame_mismatch"


def test_bad_frame_shape_is_reported(client, cfg, session):
    """delay 變成字串 = pixiv 改了回應形狀，要有人知道。"""
    m = make_media(cfg, session, meta={"frames": [{"file": "a.jpg", "delay": "40"}]})
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 500
    assert r.json()["code"] == "ugoira.bad_frame_data"


def test_broken_zip_is_reported(client, cfg, session):
    m = make_media(cfg, session, zip_ok=False)
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 500
    assert r.json()["code"] == "ugoira.unreadable"


def test_not_downloaded_yet(client, cfg, session):
    m = make_media(cfg, session)
    m.local_path = None
    session.commit()
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 409
    assert r.json()["code"] == "media.not_downloaded"


def test_path_outside_media_roots_is_rejected(client, cfg, session, tmp_path):
    """`local_path` 一旦被污染就是任意檔案讀取的入口，這裡也要擋。"""
    outside = tmp_path / "elsewhere" / "secret.zip"
    write_zip(outside)
    m = make_media(cfg, session)
    m.local_path = str(outside)
    session.commit()
    r = client.get(f"/api/media/{m.id}/ugoira")
    assert r.status_code == 403
    assert r.json()["code"] == "file.outside_root"


def test_unknown_media_is_404(client):
    r = client.get("/api/media/9999/ugoira")
    assert r.status_code == 404
    assert r.json()["code"] == "media.not_found"
