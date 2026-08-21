"""檔名產生與 Windows 安全化。這些坑在 Windows 上會真的炸。"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from snsmediadl.fspath import for_io
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


# ── 長路徑（MAX_PATH）─────────────────────────────────

@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH 是 Windows 的事")
def test_collision_is_detected_on_a_long_path(tmp_path):
    r"""⚠️ 這一條守的是**靜默覆蓋**，不是「看不到圖」。

    沒有 `\?\` 前綴時，Windows 對超過 260 字元的既有檔案回「不存在」——
    `resolve_collision` 於是回報「沒有衝突」，寫入端就直接蓋掉一個已經在的檔案。
    使用者會少一張圖，而且完全沒有訊息。
    """
    deep = tmp_path
    while len(str(deep)) < 240:
        deep = deep / ("d" * 40)
    target = deep / ("n" * 20 + ".jpg")
    assert len(str(target)) > 260, f"沒墊夠長：{len(str(target))}"

    for_io(deep).mkdir(parents=True, exist_ok=True)
    assert resolve_collision(target) == target      # 還沒有人佔用

    for_io(target).write_bytes(b"already-here")
    assert not target.exists(), "沒有前綴時 Windows 說這個檔不存在 —— 這正是危險所在"

    got = resolve_collision(target)
    assert got != target, "長路徑的既有檔案被當成不存在，寫入端會覆蓋它"
    assert got.name.endswith("_1.jpg")
    assert for_io(target).read_bytes() == b"already-here"
