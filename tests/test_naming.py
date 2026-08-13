"""檔名產生與 Windows 安全化。這些坑在 Windows 上會真的炸。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from snsmediadl.naming import (
    build_target_path,
    build_tokens,
    render_filename,
    resolve_collision,
    sanitize_component,
    split_url_filename,
)


@pytest.mark.parametrize("raw,expected", [
    ('a/b\\c:d*e?f"g<h>i|j', "a_b_c_d_e_f_g_h_i_j"),
    ("trailing dots...", "trailing dots"),
    ("trailing space   ", "trailing space"),
    ("with\x00control", "with_control"),
])
def test_illegal_characters_replaced(raw, expected):
    assert sanitize_component(raw) == expected


@pytest.mark.parametrize("name", ["CON", "con", "NUL.jpg", "COM1", "LPT9.mp4"])
def test_windows_reserved_names_get_prefixed(name):
    out = sanitize_component(name)
    assert out.startswith("_")


def test_empty_after_cleaning_falls_back():
    assert sanitize_component("...", fallback="untitled") == "untitled"
    assert sanitize_component("") == "untitled"


def test_overlong_name_truncated():
    out = sanitize_component("x" * 500)
    assert len(out) <= 120


@pytest.mark.parametrize("url,stem,ext", [
    ("https://pbs.twimg.com/media/SAMPLEPHOTO0004.jpg?name=orig", "SAMPLEPHOTO0004", "jpg"),
    ("https://video.twimg.com/amplify_video/1/vid/avc1/480x360/abc.mp4?tag=14", "abc", "mp4"),
    ("https://video.twimg.com/tweet_video/SAMPLEGIF000002.mp4", "SAMPLEGIF000002", "mp4"),
])
def test_url_filename_ignores_query_string(url, stem, ext):
    assert split_url_filename(url) == (stem, ext)


def test_tokens_from_real_photo_url():
    tokens = build_tokens(
        platform="x",
        post_id="1000000000000000003",
        ordinal=0,
        kind="photo",
        source_url="https://pbs.twimg.com/media/SAMPLEPHOTO0004.jpg?name=orig",
        posted_at=datetime(2025, 7, 8, 11, 43, 52, tzinfo=timezone.utc),
        screen_name="sample_account",
    )
    assert tokens["ext"] == "jpg"
    assert tokens["filename"] == "SAMPLEPHOTO0004"
    assert tokens["date"] == "2025-07-08 11-43-52"


def test_missing_date_does_not_crash():
    tokens = build_tokens(
        platform="x", post_id="1", ordinal=0, kind="photo",
        source_url="https://x/a.jpg",
    )
    assert tokens["date"] == "unknown-date"


def test_default_format_includes_post_id_to_avoid_cross_post_collision():
    """不同貼文可能用到同名檔案，預設 format 必須帶 post_id。"""
    fmt = "[%date%] %post_id%_%ordinal%.%ext%"
    a = render_filename(fmt, build_tokens(
        platform="x", post_id="111", ordinal=0, kind="photo",
        source_url="https://x/same.jpg",
        posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    b = render_filename(fmt, build_tokens(
        platform="x", post_id="222", ordinal=0, kind="photo",
        source_url="https://x/same.jpg",
        posted_at=datetime(2025, 1, 1, tzinfo=timezone.utc)))
    assert a != b


def test_unknown_token_left_intact():
    out = render_filename("%post_id%-%nope%.%ext%", build_tokens(
        platform="x", post_id="1", ordinal=0, kind="photo",
        source_url="https://x/a.jpg"))
    assert "%nope%" in out


def test_collision_appends_suffix(tmp_path):
    target = tmp_path / "a.jpg"
    assert resolve_collision(target) == target

    target.write_bytes(b"x")
    first = resolve_collision(target)
    assert first.name == "a_1.jpg"

    first.write_bytes(b"x")
    assert resolve_collision(target).name == "a_2.jpg"


def test_target_path_groups_by_platform_and_account(tmp_path):
    tokens = build_tokens(
        platform="x", post_id="1", ordinal=0, kind="photo",
        source_url="https://x/a.jpg", screen_name="sample_account")
    p = build_target_path(
        output_root=tmp_path, fmt="%post_id%.%ext%", tokens=tokens)
    assert p.parent == tmp_path / "x" / "sample_account"
    assert p.name == "1.jpg"


def test_target_path_flat_when_grouping_disabled(tmp_path):
    tokens = build_tokens(
        platform="x", post_id="1", ordinal=0, kind="photo",
        source_url="https://x/a.jpg", screen_name="acct")
    p = build_target_path(
        output_root=tmp_path, fmt="%post_id%.%ext%", tokens=tokens,
        group_by_account=False)
    assert p.parent == tmp_path


def test_malicious_screen_name_cannot_escape_output_root(tmp_path):
    """帳號名來自平台，不可信 —— 不能靠它跳出輸出目錄。"""
    tokens = build_tokens(
        platform="x", post_id="1", ordinal=0, kind="photo",
        source_url="https://x/a.jpg", screen_name="../../etc")
    p = build_target_path(
        output_root=tmp_path, fmt="%post_id%.%ext%", tokens=tokens)
    assert tmp_path in p.parents
