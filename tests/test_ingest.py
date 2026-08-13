"""Ingest：增量去重與分級繼承。"""

from __future__ import annotations

from sqlalchemy import func, select

from snsmediadl.db.models import Account, Media, Post
from snsmediadl.services.ingest import ingest

MIXED_POST = "1000000000000000003"


def _counts(session):
    return (
        session.scalar(select(func.count()).select_from(Post)),
        session.scalar(select(func.count()).select_from(Media)),
    )


def test_ingest_real_capture(session, sample_account):
    r = ingest(session, "x", sample_account, screen_name="sample_account")
    assert (r.posts_new, r.media_new, r.posts_skipped) == (4, 6, 0)
    assert _counts(session) == (4, 6)


def test_reingest_is_a_noop(session, sample_account):
    """增量驗證：同一份資料再灌一次，新增 0 筆。"""
    ingest(session, "x", sample_account, screen_name="sample_account")
    r = ingest(session, "x", sample_account, screen_name="sample_account")
    assert (r.posts_new, r.media_new) == (0, 0)
    assert r.posts_skipped == 4
    assert _counts(session) == (4, 6)


def test_mixed_kind_post_stored_correctly(session, sample_account):
    ingest(session, "x", sample_account, screen_name="sample_account")
    post = session.scalar(select(Post).where(Post.platform_post_id == MIXED_POST))
    kinds = sorted(m.kind for m in post.media)
    assert kinds == ["animated_gif", "photo", "video"]


def test_account_created_once(session, sample_account):
    ingest(session, "x", sample_account, screen_name="sample_account")
    ingest(session, "x", sample_account, screen_name="sample_account")
    assert session.scalar(select(func.count()).select_from(Account)) == 1


def test_mixed_account_batch_does_not_mislabel(session):
    """一批貼文來自多個帳號時，screen_name 不可套到所有帳號上。

    screen_name 是 request 層級的，帳號卻是每則貼文自己的 —— 照套下去會把
    第二個帳號的名字寫成第一個的。那是寫壞資料，不只是顯示錯。
    """
    payload = [
        {"postId": "a1", "userId": "u1", "createdAt": None,
         "media": [{"kind": "photo", "url": "x", "orig": "x?name=orig"}]},
        {"postId": "b1", "userId": "u2", "createdAt": None,
         "media": [{"kind": "photo", "url": "y", "orig": "y?name=orig"}]},
    ]
    ingest(session, "x", payload, screen_name="alice")

    names = {a.platform_user_id: a.screen_name for a in session.scalars(select(Account))}
    assert names == {"u1": None, "u2": None}, "混帳號批次不該套用 screen_name"


def test_single_account_batch_still_gets_name(session):
    payload = [{
        "postId": "a1", "userId": "u1", "createdAt": None,
        "media": [{"kind": "photo", "url": "x", "orig": "x?name=orig"}],
    }]
    ingest(session, "x", payload, screen_name="alice")
    assert session.scalar(select(Account)).screen_name == "alice"


def test_screen_name_updated_on_rename(session, sample_account):
    ingest(session, "x", sample_account, screen_name="old_name")
    ingest(session, "x", sample_account, screen_name="new_name")
    acc = session.scalar(select(Account))
    assert acc.screen_name == "new_name"


# --- rating 繼承的四種優先序 ---

def _one_post(**extra):
    return [{
        "postId": "p1", "userId": "u1", "createdAt": None,
        "media": [{"kind": "photo", "url": "https://x/a.jpg", "orig": "https://x/a.jpg?name=orig"}],
        **extra,
    }]


def test_rating_priority_1_explicit_is_manual(session):
    ingest(session, "x", _one_post(rating="r18", contentType="ai"))
    p = session.scalar(select(Post))
    assert (p.rating, p.content_type, p.rating_source) == ("r18", "ai", "manual")


def test_rating_priority_2_account_default(session):
    session.add(Account(platform="x", platform_user_id="u1",
                        default_rating="r18", default_content_type="illust"))
    session.commit()
    ingest(session, "x", _one_post())
    p = session.scalar(select(Post))
    assert (p.rating, p.content_type, p.rating_source) == ("r18", "illust", "account_default")


def test_rating_priority_3_sensitive_hint_is_auto(session):
    ingest(session, "x", _one_post(possiblySensitive=True))
    p = session.scalar(select(Post))
    assert (p.rating, p.rating_source) == ("r18", "auto")


def test_rating_priority_4_unknown_stays_null(session):
    """沒有任何線索時留 NULL，不預設 sfw。"""
    ingest(session, "x", _one_post())
    p = session.scalar(select(Post))
    assert p.rating is None
    assert p.content_type is None
    assert p.rating_source is None


def test_explicit_beats_account_default(session):
    session.add(Account(platform="x", platform_user_id="u1", default_rating="sfw"))
    session.commit()
    ingest(session, "x", _one_post(rating="r18"))
    p = session.scalar(select(Post))
    assert (p.rating, p.rating_source) == ("r18", "manual")


def test_account_default_beats_sensitive_hint(session):
    session.add(Account(platform="x", platform_user_id="u1", default_rating="sfw"))
    session.commit()
    ingest(session, "x", _one_post(possiblySensitive=True))
    p = session.scalar(select(Post))
    assert (p.rating, p.rating_source) == ("sfw", "account_default")


def test_media_meta_persisted(session, sample_account):
    ingest(session, "x", sample_account, screen_name="sample_account")
    vid = session.scalar(select(Media).where(Media.kind == "video"))
    assert vid.meta_json is not None
    assert "bitrate" in vid.meta_json


def test_all_media_start_pending(session, sample_account):
    ingest(session, "x", sample_account, screen_name="sample_account")
    statuses = {m.status for m in session.scalars(select(Media))}
    assert statuses == {"pending"}
