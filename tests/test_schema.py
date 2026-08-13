"""schema 的結構性保證。這些是設計決定，不是實作細節 —— 改動要先改計畫。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from snsmediadl.db.models import Account, Creator, Media, Post
from snsmediadl.db.enums import AccountRole, MediaKind, MediaStatus, Rating


def test_all_tables_created(engine):
    names = set(inspect(engine).get_table_names())
    assert names == {"creators", "accounts", "posts", "media"}


def test_post_dedupe_key_is_platform_and_post_id(session):
    acc = Account(platform="x", platform_user_id="u1")
    session.add(acc)
    session.flush()

    session.add(Post(platform="x", platform_post_id="p1", account_id=acc.id))
    session.commit()

    session.add(Post(platform="x", platform_post_id="p1", account_id=acc.id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_same_creator_can_own_two_accounts_on_same_platform(session):
    """本帳 + 小帳同平台 —— accounts 的唯一鍵刻意不含 creator_id。"""
    c = Creator(display_name="某畫師")
    session.add(c)
    session.flush()

    session.add_all([
        Account(platform="x", platform_user_id="1", screen_name="artist",
                creator_id=c.id, role=AccountRole.MAIN.value),
        Account(platform="x", platform_user_id="2", screen_name="artist_r18",
                creator_id=c.id, role=AccountRole.R18_ALT.value),
    ])
    session.commit()

    session.refresh(c)
    assert len(c.accounts) == 2
    assert {a.role for a in c.accounts} == {"main", "r18_alt"}


def test_creator_spans_platforms(session):
    c = Creator(display_name="某畫師")
    session.add(c)
    session.flush()
    session.add_all([
        Account(platform="x", platform_user_id="1", creator_id=c.id),
        Account(platform="pixiv", platform_user_id="99", creator_id=c.id),
        Account(platform="misskey", platform_user_id="m1", creator_id=c.id),
    ])
    session.commit()
    session.refresh(c)
    assert {a.platform for a in c.accounts} == {"x", "pixiv", "misskey"}


def test_one_post_can_hold_mixed_media_kinds(session):
    """實測發現：單一貼文可同時含 photo + video + animated_gif。"""
    acc = Account(platform="x", platform_user_id="u1")
    session.add(acc)
    session.flush()
    p = Post(platform="x", platform_post_id="mixed", account_id=acc.id)
    session.add(p)
    session.flush()

    session.add_all([
        Media(post_id=p.id, ordinal=0, kind=MediaKind.PHOTO.value, source_url="a"),
        Media(post_id=p.id, ordinal=1, kind=MediaKind.VIDEO.value, source_url="b"),
        Media(post_id=p.id, ordinal=2, kind=MediaKind.ANIMATED_GIF.value, source_url="c"),
    ])
    session.commit()

    session.refresh(p)
    assert {m.kind for m in p.media} == {"photo", "video", "animated_gif"}


def test_rating_check_constraint_rejects_garbage(session):
    acc = Account(platform="x", platform_user_id="u1")
    session.add(acc)
    session.flush()
    session.add(Post(platform="x", platform_post_id="p1", account_id=acc.id,
                     rating="totally-not-a-rating"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_rating_defaults_to_null_not_sfw(session):
    """未知就是未知。預設 sfw 等於用假資料兜底。"""
    acc = Account(platform="x", platform_user_id="u1")
    session.add(acc)
    session.flush()
    p = Post(platform="x", platform_post_id="p1", account_id=acc.id)
    session.add(p)
    session.commit()
    assert p.rating is None
    assert p.rating_source is None


def test_media_defaults_to_pending(session):
    acc = Account(platform="x", platform_user_id="u1")
    session.add(acc)
    session.flush()
    p = Post(platform="x", platform_post_id="p1", account_id=acc.id)
    session.add(p)
    session.flush()
    m = Media(post_id=p.id, ordinal=0, kind=MediaKind.PHOTO.value, source_url="u")
    session.add(m)
    session.commit()
    assert m.status == MediaStatus.PENDING.value
    assert m.attempt_count == 0


def test_foreign_keys_enforced(session):
    session.add(Post(platform="x", platform_post_id="orphan", account_id=9999))
    with pytest.raises(IntegrityError):
        session.commit()


def test_rating_values_are_only_sfw_and_r18():
    assert Rating.values() == ["sfw", "r18"]
