"""刪除記錄：連鎖範圍、confirm 閘門、**檔案必須留著**。

最重要的那條斷言是 `test_deleting_an_account_never_touches_the_files`：
它實際在磁碟上建檔，刪完之後檢查檔案還在。
斷言「我們沒呼叫 unlink」是不夠的 —— 那只證明我們沒用那一個 API。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from snsmediadl.api.app import create_app
from snsmediadl.db.enums import MediaStatus
from snsmediadl.db.models import Account, Creator, Media, Post
from snsmediadl.services import deletion


def _account(session, *, platform="x", user_id="1", name="artist", creator=None):
    account = Account(
        platform=platform,
        platform_user_id=user_id,
        screen_name=name,
        creator_id=creator.id if creator else None,
    )
    session.add(account)
    session.flush()
    return account


def _post_with_media(session, account, post_id: str, *, files: list = (), status=None):
    post = Post(
        platform=account.platform,
        platform_post_id=post_id,
        account_id=account.id,
    )
    session.add(post)
    session.flush()
    for i, path in enumerate(files):
        session.add(
            Media(
                post_id=post.id,
                ordinal=i,
                kind="photo",
                source_url=f"https://example.invalid/{post_id}_{i}.jpg",
                local_path=str(path) if path else None,
                file_hash="deadbeef" if path else None,
                status=status or (
                    MediaStatus.DONE.value if path else MediaStatus.PENDING.value
                ),
            )
        )
    session.flush()
    return post


# ── 服務層 ───────────────────────────────────────────────


def test_deleting_an_account_never_touches_the_files(session, tmp_path):
    """使用者的明確要求：只刪資料，不刪檔案。"""
    files = []
    for i in range(3):
        f = tmp_path / f"pic{i}.jpg"
        f.write_bytes(b"not really a jpeg")
        files.append(f)

    account = _account(session)
    _post_with_media(session, account, "p1", files=files[:2])
    _post_with_media(session, account, "p2", files=files[2:])
    session.commit()

    summary = deletion.delete_account(session, account.id)

    assert summary.posts == 2
    assert summary.media == 3
    assert summary.downloaded_files_kept == 3
    # ⭐ 這三行是這個功能的安全底線
    for f in files:
        assert f.exists(), f"{f} 被刪掉了 —— 刪除功能不該碰任何檔案"


def test_account_deletion_removes_posts_and_media_records(session):
    account = _account(session)
    _post_with_media(session, account, "p1", files=[None, None])
    session.commit()

    deletion.delete_account(session, account.id)

    assert session.scalars(select(Account)).all() == []
    assert session.scalars(select(Post)).all() == []
    assert session.scalars(select(Media)).all() == []


def test_other_accounts_are_untouched(session):
    keep = _account(session, user_id="1", name="keep")
    drop = _account(session, user_id="2", name="drop")
    _post_with_media(session, keep, "k1", files=[None])
    _post_with_media(session, drop, "d1", files=[None])
    session.commit()

    deletion.delete_account(session, drop.id)

    accounts = session.scalars(select(Account)).all()
    assert [a.screen_name for a in accounts] == ["keep"]
    assert len(session.scalars(select(Post)).all()) == 1
    assert len(session.scalars(select(Media)).all()) == 1


def test_creator_survives_account_deletion(session):
    """一位創作者可以有多個帳號。刪一個帳號不代表這個人不存在了。"""
    creator = Creator(display_name="某位繪師")
    session.add(creator)
    session.flush()
    account = _account(session, creator=creator)
    session.commit()

    deletion.delete_account(session, account.id)

    assert session.get(Creator, creator.id) is not None


def test_preview_does_not_write_anything(session):
    account = _account(session)
    _post_with_media(session, account, "p1", files=[None, None])
    session.commit()

    preview = deletion.preview_account_deletion(session, account.id)

    assert preview.posts == 1
    assert preview.media == 2
    assert session.get(Account, account.id) is not None
    assert len(session.scalars(select(Post)).all()) == 1


def test_preview_warns_about_redownload_becoming_duplicates(session, tmp_path):
    """刪除的真實後果要講出來，不能等使用者三個月後發現每張圖有兩份。"""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    account = _account(session)
    _post_with_media(session, account, "p1", files=[f])
    session.commit()

    preview = deletion.preview_account_deletion(session, account.id)

    assert preview.downloaded_files_kept == 1
    assert any("副本" in w for w in preview.warnings)


def test_interrupted_downloads_are_reported(session, tmp_path):
    account = _account(session)
    _post_with_media(
        session, account, "p1", files=[None], status=MediaStatus.DOWNLOADING.value
    )
    session.commit()

    preview = deletion.preview_account_deletion(session, account.id)

    assert preview.interrupted_downloads == 1
    assert any("打斷" in w for w in preview.warnings)


def test_deleting_a_post_keeps_the_account(session):
    account = _account(session)
    post = _post_with_media(session, account, "p1", files=[None])
    _post_with_media(session, account, "p2", files=[None])
    session.commit()

    summary = deletion.delete_post(session, post.id)

    assert summary.posts == 1
    assert summary.media == 1
    assert session.get(Account, account.id) is not None
    assert len(session.scalars(select(Post)).all()) == 1


def test_deleting_one_media_keeps_the_post(session):
    account = _account(session)
    _post_with_media(session, account, "p1", files=[None, None])
    session.commit()
    media = session.scalars(select(Media)).first()

    deletion.delete_media(session, media.id)

    assert len(session.scalars(select(Media)).all()) == 1
    assert len(session.scalars(select(Post)).all()) == 1


def test_missing_account_raises_lookup_error(session):
    with pytest.raises(LookupError):
        deletion.preview_account_deletion(session, 12345)


# ── 端點 ─────────────────────────────────────────────────


def test_missing_account_is_404_not_a_silent_noop(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.delete("/api/accounts/999").status_code == 404
        assert client.get("/api/accounts/999/deletion-preview").status_code == 404


def test_delete_endpoint_flow(cfg, sample_account):
    app = create_app(cfg)
    with TestClient(app) as client:
        client.post("/api/ingest",
                    json={"platform": "x", "screenName": "sample_account",
                          "posts": sample_account})
        accounts = client.get("/api/accounts").json()
        account_id = accounts[0]["id"]

        preview = client.get(f"/api/accounts/{account_id}/deletion-preview").json()
        assert preview["posts"] > 0
        assert preview["media"] > 0

        # 沒帶 confirm：擋下來，而且要把數字講出來
        blocked = client.delete(f"/api/accounts/{account_id}")
        assert blocked.status_code == 400
        assert str(preview["posts"]) in blocked.json()["detail"]

        ok = client.delete(f"/api/accounts/{account_id}?confirm=true")
        assert ok.status_code == 200
        assert ok.json()["posts"] == preview["posts"]

        assert client.get("/api/accounts").json() == []
