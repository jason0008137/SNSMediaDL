"""網址解析。純函式，一個網路請求都不該發生。"""

from __future__ import annotations

import pytest

from snsmediadl.urls import ParseError, parse_lines, parse_target


def t(line: str) -> tuple[str, str, str]:
    target = parse_target(line)
    return (target.platform, target.host, target.acct)


# ── 認得的形式 ──────────────────────────────────────────

@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # misskey
        ("https://misskey.io/@someone", ("misskey", "misskey.io", "someone")),
        ("https://misskey.io/@someone/", ("misskey", "misskey.io", "someone")),
        ("misskey.io/@someone", ("misskey", "misskey.io", "someone")),
        ("http://misskey.io/@someone", ("misskey", "misskey.io", "someone")),
        ("HTTPS://MISSKEY.IO/@Someone", ("misskey", "misskey.io", "Someone")),
        # mastodon（baraag）—— 媒體分頁與回覆分頁都要吃得下
        ("https://baraag.net/@artist", ("mastodon", "baraag.net", "artist")),
        ("https://baraag.net/@artist/media", ("mastodon", "baraag.net", "artist")),
        ("https://baraag.net/@artist/with_replies", ("mastodon", "baraag.net", "artist")),
        ("https://baraag.net/@artist?x=1", ("mastodon", "baraag.net", "artist")),
        # 裸帳號
        ("@artist@baraag.net", ("mastodon", "baraag.net", "artist")),
        ("artist@baraag.net", ("mastodon", "baraag.net", "artist")),
        # pixiv —— 單一站台，host 是空字串
        ("https://www.pixiv.net/users/12345", ("pixiv", "", "12345")),
        ("https://www.pixiv.net/en/users/12345", ("pixiv", "", "12345")),
        ("https://pixiv.net/users/12345/artworks", ("pixiv", "", "12345")),
        ("https://www.pixiv.net/member.php?id=12345", ("pixiv", "", "12345")),
    ],
)
def test_parses(line: str, expected: tuple[str, str, str]) -> None:
    assert t(line) == expected


def test_username_case_is_preserved_but_key_is_not() -> None:
    """顯示要保留原樣（平台會顯示大小寫），去重要不分大小寫。"""
    a = parse_target("https://misskey.io/@SomeOne")
    b = parse_target("https://misskey.io/@someone")
    assert a.acct == "SomeOne"
    assert a.key == b.key


def test_platform_override_opens_other_instances() -> None:
    """沒有硬編在表裡的 instance 靠覆寫語法進來。"""
    assert t("misskey|https://misskey.design/@someone") == (
        "misskey", "misskey.design", "someone",
    )
    assert t("mastodon|https://pawoo.net/@someone") == (
        "mastodon", "pawoo.net", "someone",
    )


def test_pixiv_host_is_empty_string_not_none() -> None:
    """⚠️ 單一站台平台一律空字串。

    NULL / None 會讓含 instance_host 的唯一索引形同虛設
    （SQLite 把每個 NULL 當成不同的值）。
    """
    assert parse_target("https://www.pixiv.net/users/1").host == ""


# ── 明確拒絕 ────────────────────────────────────────────

@pytest.mark.parametrize(
    ("line", "must_mention"),
    [
        # X 要講出替代方案，不能只說不支援
        ("https://x.com/someone", "extension"),
        ("https://twitter.com/someone", "extension"),
        ("https://www.instagram.com/someone", "recon"),
        # 不認得的站台要給覆寫語法
        ("https://example.com/@someone", "misskey|"),
        # 遠端使用者：抓下來會掛在錯的 instance 底下，變成同一人兩筆帳號
        ("https://baraag.net/@someone@misskey.io", "misskey.io"),
        # 作品網址不是作者頁
        ("https://www.pixiv.net/artworks/999", "users/"),
        # misskey 的 id 形式網址（adapter 是用 username 查的）
        ("https://misskey.io/users/9abcdef", "@帳號"),
        # 缺帳號
        ("https://misskey.io/", "沒有帳號"),
        ("https://baraag.net/@", "沒有帳號名"),
        # 平台名打錯
        ("mistake|https://misskey.io/@a", "不認得平台"),
        ("", "空白"),
    ],
)
def test_rejects(line: str, must_mention: str) -> None:
    with pytest.raises(ParseError) as exc:
        parse_target(line)
    assert must_mention in str(exc.value)


def test_pixiv_non_numeric_id_is_rejected() -> None:
    """pixiv 的使用者名稱可以改而且不唯一 —— 用它當身分會抓錯人。"""
    with pytest.raises(ParseError, match="數字"):
        parse_target("https://www.pixiv.net/users/someone")


# ── 批次 ────────────────────────────────────────────────

def test_parse_lines_keeps_going_after_a_bad_line() -> None:
    """一行看不懂不可以影響其他行 —— 這正是批次的重點。"""
    lines = parse_lines(
        "\n".join([
            "https://misskey.io/@a",
            "https://x.com/b",            # 拒絕
            "  ",                          # 略過
            "# 這是註解",                   # 略過
            "https://baraag.net/@c/media",
            "https://misskey.io/@A",       # 與第一行重複（大小寫不同）
        ])
    )
    assert len(lines) == 4
    assert [ln.target.acct if ln.target else None for ln in lines] == ["a", None, "c", "A"]
    assert lines[1].error is not None and "extension" in lines[1].error
    assert [ln.duplicate for ln in lines] == [False, False, False, True]


def test_parse_lines_ignores_blank_and_comments_entirely() -> None:
    assert parse_lines("\n\n  \n# nope\n") == []
