"""平台 → 網址（`snsmediadl/links.py`）。

這些測試的重點**不是**「網址拼對了」，而是「拼不出來的時候有沒有誠實說」。
連到錯的地方比 404 更糟：404 使用者會回報，連到 x.com 上一個不存在的
misskey 貼文，使用者只會以為那個作者刪文了。
"""

from __future__ import annotations

import pytest

from snsmediadl.links import display_name, post_url, profile_url


# ── 帳號頁：四個平台各自的正常形狀 ───────────────────────
@pytest.mark.parametrize(
    "platform, host, user_id, screen_name, expected",
    [
        ("x", "", "123", "someone", "https://x.com/someone"),
        ("misskey", "misskey.io", "9abc", "someone", "https://misskey.io/@someone"),
        ("mastodon", "baraag.net", "42", "someone", "https://baraag.net/@someone"),
        ("pixiv", "", "12345", "作者", "https://www.pixiv.net/users/12345"),
    ],
)
def test_profile_url_normal(platform, host, user_id, screen_name, expected):
    url, problem = profile_url(platform, host, user_id, screen_name)
    assert url == expected
    assert problem is None


# ── 貼文頁：四個平台的形狀彼此**不一樣**，這正是寫死一種會出事的原因 ──
@pytest.mark.parametrize(
    "platform, host, post_id, screen_name, expected",
    [
        ("x", "", "999", "someone", "https://x.com/someone/status/999"),
        # misskey 的貼文網址不含帳號名
        ("misskey", "misskey.io", "9note", "someone", "https://misskey.io/notes/9note"),
        # mastodon 相反，要 /@user/id
        ("mastodon", "baraag.net", "77", "someone", "https://baraag.net/@someone/77"),
        ("pixiv", "", "88", "作者", "https://www.pixiv.net/artworks/88"),
    ],
)
def test_post_url_normal(platform, host, post_id, screen_name, expected):
    url, problem = post_url(platform, host, post_id, screen_name)
    assert url == expected
    assert problem is None


# ── 缺欄位：回問題，不回一個「大概對」的網址 ──────────────
def test_profile_url_missing_screen_name():
    url, problem = profile_url("x", "", "123", None)
    assert url is None
    assert "screen_name" in problem


def test_profile_url_missing_host():
    url, problem = profile_url("misskey", "", "9abc", "someone")
    assert url is None
    assert "instance_host" in problem


def test_post_url_missing_post_id():
    url, problem = post_url("x", "", None, "someone")
    assert url is None
    assert "platform_post_id" in problem


def test_post_url_mastodon_missing_screen_name():
    """mastodon 少了 screen_name 就真的拼不出來（misskey 不受影響）。"""
    url, problem = post_url("mastodon", "baraag.net", "77", None)
    assert url is None
    assert "screen_name" in problem

    url, problem = post_url("misskey", "misskey.io", "9note", None)
    assert url == "https://misskey.io/notes/9note"
    assert problem is None


# ── `sn:` 哨符 ────────────────────────────────────────
def test_profile_url_pixiv_placeholder_explains_itself():
    """匯入來的 pixiv 帳號沒有真 id —— 要說「抓一次會自動治療」，
    不是丟一個 https://www.pixiv.net/users/sn:東西 這種壞網址。"""
    url, problem = profile_url("pixiv", "", "sn:東西", "東西")
    assert url is None
    assert "user id" in problem
    assert "sn:東西" not in problem       # 不把哨符當成 id 講給使用者聽


def test_post_url_pixiv_placeholder_account_still_links():
    """作品網址只認作品 id，與帳號 id 無關 —— 哨符帳號的**貼文**仍連得過去。

    這一條刻意存在：為了對稱而把哨符檢查複製到 post_url，會讓 23 個
    本來連得到的作品變成連不到。
    """
    url, problem = post_url("pixiv", "", "88", "東西")
    assert url == "https://www.pixiv.net/artworks/88"
    assert problem is None


def test_profile_url_pixiv_non_numeric_id():
    url, problem = profile_url("pixiv", "", "abc", "someone")
    assert url is None
    assert "數字" in problem


# ── 未知平台 ──────────────────────────────────────────
def test_baraag_is_not_silently_treated_as_mastodon():
    """`platform = 'baraag'` 代表 migration 沒跑完。

    **不可以偷偷當成 mastodon** —— 那會讓「沒跑 migration」這件事永遠
    不被發現，正是根因原則要防的掩蓋。
    """
    url, problem = profile_url("baraag", "baraag.net", "42", "someone")
    assert url is None
    assert "migration" in problem

    url, problem = post_url("baraag", "baraag.net", "77", "someone")
    assert url is None
    assert "migration" in problem


def test_unknown_platform_says_so():
    url, problem = profile_url("bluesky", "bsky.app", "1", "someone")
    assert url is None
    assert "bluesky" in problem


# ── 顯示名 ────────────────────────────────────────────
def test_display_name_uses_instance_host_for_fediverse():
    """使用者認得的是 misskey.io，不是「misskey」這個軟體名。"""
    assert display_name("misskey", "misskey.io") == "misskey.io"
    assert display_name("mastodon", "baraag.net") == "baraag.net"
    assert display_name("x", "") == "X"
    assert display_name("pixiv", "") == "pixiv"
    # 未知平台照原字串顯示，不猜
    assert display_name("bluesky", "") == "bluesky"
