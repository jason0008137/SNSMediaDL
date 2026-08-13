"""X adapter 對真實捕獲資料的解析。"""

from __future__ import annotations

from datetime import timezone

from snsmediadl.adapters import get_adapter
from snsmediadl.adapters.x import XAdapter

MIXED_POST = "1000000000000000003"


def test_normalize_real_capture(sample_account):
    posts = XAdapter().normalize(sample_account)
    assert len(posts) == 4
    assert sum(len(p.media) for p in posts) == 6


def test_mixed_kind_post_keeps_all_three_kinds(sample_account):
    posts = XAdapter().normalize(sample_account)
    mixed = next(p for p in posts if p.platform_post_id == MIXED_POST)
    assert [m.kind for m in mixed.media] == ["photo", "video", "animated_gif"]
    assert [m.ordinal for m in mixed.media] == [0, 1, 2]


def test_photo_uses_orig_url(sample_account):
    posts = XAdapter().normalize(sample_account)
    photo = next(
        m for p in posts for m in p.media if m.kind == "photo"
    )
    assert photo.source_url.endswith("?name=orig")


def test_video_keeps_query_string(sample_account):
    """影片 URL 的 ?tag=14 不可剝掉。"""
    posts = XAdapter().normalize(sample_account)
    video = next(m for p in posts for m in p.media if m.kind == "video")
    assert "?tag=" in video.source_url
    assert video.source_url.split("?")[0].endswith(".mp4")


def test_video_meta_carries_bitrate_and_duration(sample_account):
    posts = XAdapter().normalize(sample_account)
    video = next(m for p in posts for m in p.media if m.kind == "video")
    assert video.meta["bitrate"] == 832000
    assert video.meta["durationMs"] == 4736 or video.meta["durationMs"] == 4783


def test_twitter_date_parsed_with_timezone(sample_account):
    posts = XAdapter().normalize(sample_account)
    p = next(p for p in posts if p.platform_post_id == MIXED_POST)
    assert p.posted_at is not None
    assert p.posted_at.tzinfo is not None
    assert p.posted_at.astimezone(timezone.utc).year == 2025


def test_unparseable_date_yields_none_not_crash():
    posts = XAdapter().normalize([
        {"postId": "1", "userId": "u", "createdAt": "not a date",
         "media": [{"kind": "photo", "url": "u", "orig": "u?name=orig"}]},
    ])
    assert posts[0].posted_at is None


def test_unknown_media_kind_is_skipped():
    posts = XAdapter().normalize([
        {"postId": "1", "userId": "u", "createdAt": None,
         "media": [{"kind": "hologram", "url": "u"},
                   {"kind": "photo", "url": "p", "orig": "p?name=orig"}]},
    ])
    assert [m.kind for m in posts[0].media] == ["photo"]


def test_post_without_media_is_dropped():
    posts = XAdapter().normalize([{"postId": "1", "userId": "u", "media": []}])
    assert posts == []


def test_sensitive_hint_passed_through():
    posts = XAdapter().normalize([
        {"postId": "1", "userId": "u", "possiblySensitive": True,
         "media": [{"kind": "photo", "url": "p", "orig": "p?name=orig"}]},
    ])
    assert posts[0].sensitive_hint is True


def test_x_needs_no_auth_headers():
    """實測 12/12：X 媒體 URL 不需要 Referer 或憑證。"""
    headers = XAdapter().download_headers("https://pbs.twimg.com/media/x.jpg")
    assert "Referer" not in headers
    assert "Cookie" not in headers
    assert "Authorization" not in headers


def test_registry_returns_x_adapter():
    assert get_adapter("x").platform == "x"
