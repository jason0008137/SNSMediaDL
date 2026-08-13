"""Misskey / Mastodon adapter 的正規化。

fixture 是**手寫的最小回應**，不是真實抓包 —— 避免憑證與個資進版控。
欄位名依官方 API 文件。
"""

from __future__ import annotations

import pytest

from snsmediadl.adapters import MastodonAdapter, MisskeyAdapter
from snsmediadl.adapters.mastodon import MastodonFieldError
from snsmediadl.adapters.misskey import MisskeyFieldError

# ── Misskey ───────────────────────────────────────────

MSK_NOTE = {
    "id": "9abc001",
    "createdAt": "2026-08-01T10:00:00.000Z",
    "text": "hello",
    "user": {"id": "u_777", "username": "artist"},
    "files": [
        {"id": "f1", "type": "image/png", "url": "https://files.misskey.io/a.png",
         "thumbnailUrl": "https://files.misskey.io/a-thumb.webp",
         "isSensitive": False, "size": 1234},
        {"id": "f2", "type": "video/mp4", "url": "https://files.misskey.io/b.mp4",
         "isSensitive": True},
    ],
}


def test_misskey_normalizes_files():
    posts = MisskeyAdapter().normalize([MSK_NOTE], instance_host="misskey.io")
    assert len(posts) == 1
    p = posts[0]
    assert p.platform_post_id == "9abc001"
    assert p.platform_user_id == "u_777"
    assert p.instance_host == "misskey.io"
    assert [m.kind for m in p.media] == ["photo", "video"]
    assert p.media[0].source_url == "https://files.misskey.io/a.png"


def test_misskey_uses_file_id_as_media_key():
    """Misskey 有穩定的 file id —— 比 X 好，X 只能靠 (post_id, ordinal)。"""
    p = MisskeyAdapter().normalize([MSK_NOTE])[0]
    assert [m.platform_media_key for m in p.media] == ["f1", "f2"]


def test_misskey_sensitive_is_per_file_but_rating_is_per_post():
    """敏感標記在檔案層，但分級是貼文層的屬性 —— 任一檔案敏感就算。"""
    assert MisskeyAdapter().normalize([MSK_NOTE])[0].sensitive_hint is True

    clean = {**MSK_NOTE, "files": [MSK_NOTE["files"][0]]}
    assert MisskeyAdapter().normalize([clean])[0].sensitive_hint is None


def test_misskey_mime_maps_gif_before_image():
    note = {**MSK_NOTE, "files": [
        {"id": "g", "type": "image/gif", "url": "https://x/a.gif"}]}
    assert MisskeyAdapter().normalize([note])[0].media[0].kind == "animated_gif"


def test_misskey_skips_notes_without_media():
    assert MisskeyAdapter().normalize([{**MSK_NOTE, "files": []}]) == []


def test_misskey_missing_url_raises_instead_of_guessing():
    """缺欄位通常代表平台改版。兜過去的結果是靜默漏抓。"""
    broken = {**MSK_NOTE, "files": [{"id": "f", "type": "image/png"}]}
    with pytest.raises(MisskeyFieldError, match="url"):
        MisskeyAdapter().normalize([broken])


def test_misskey_pure_renote_is_marked():
    renote = {**MSK_NOTE, "renoteId": "9zzz", "text": None}
    assert MisskeyAdapter().normalize([renote])[0].is_retweet is True
    quote = {**MSK_NOTE, "renoteId": "9zzz", "text": "我的評論"}
    assert MisskeyAdapter().normalize([quote])[0].is_retweet is False


# ── Mastodon ──────────────────────────────────────────

MST_STATUS = {
    "id": "110900001",
    "created_at": "2026-08-01T10:00:00.000Z",
    "sensitive": True,
    "reblog": None,
    "account": {"id": "acc_42", "acct": "artist"},
    "media_attachments": [
        {"id": "m1", "type": "image", "url": "https://baraag.net/media/a.png",
         "preview_url": "https://baraag.net/media/small/a.png"},
        {"id": "m2", "type": "gifv", "url": "https://baraag.net/media/b.mp4"},
        {"id": "m3", "type": "audio", "url": "https://baraag.net/media/c.mp3"},
    ],
}


def test_mastodon_normalizes_attachments():
    posts = MastodonAdapter().normalize([MST_STATUS], instance_host="baraag.net")
    p = posts[0]
    assert p.platform_post_id == "110900001"
    assert p.platform_user_id == "acc_42"
    assert p.instance_host == "baraag.net"
    # audio 沒有對應的 kind，跳過但不假裝它是圖片
    assert [m.kind for m in p.media] == ["photo", "animated_gif"]


def test_mastodon_sensitive_is_on_the_status_not_the_attachment():
    """與 Misskey 相反 —— 這個差異踩過就知道，寫成測試守住。"""
    assert MastodonAdapter().normalize([MST_STATUS])[0].sensitive_hint is True
    assert MastodonAdapter().normalize(
        [{**MST_STATUS, "sensitive": False}])[0].sensitive_hint is None


def test_mastodon_uses_url_not_preview_url():
    """preview_url 是縮圖。抓成縮圖是不會報錯的那種錯，所以要測。"""
    m = MastodonAdapter().normalize([MST_STATUS])[0].media[0]
    assert m.source_url == "https://baraag.net/media/a.png"
    assert m.meta["preview_url"].endswith("small/a.png")


def test_mastodon_reblog_takes_inner_media_and_author():
    inner = {**MST_STATUS, "id": "inner1", "account": {"id": "orig", "acct": "original"}}
    outer = {
        "id": "outer1", "created_at": "2026-08-02T00:00:00.000Z",
        "account": {"id": "booster", "acct": "booster"},
        "media_attachments": [], "sensitive": False, "reblog": inner,
    }
    p = MastodonAdapter().normalize([outer])[0]
    assert p.is_retweet is True
    # 記的是原作者與原貼文，不是轉嘟者 —— 否則作品會歸戶到轉嘟的人身上
    assert p.platform_post_id == "inner1"
    assert p.platform_user_id == "orig"


def test_mastodon_missing_url_raises():
    broken = {**MST_STATUS, "media_attachments": [{"id": "m", "type": "image"}]}
    with pytest.raises(MastodonFieldError, match="url"):
        MastodonAdapter().normalize([broken])


def test_mastodon_skips_statuses_without_usable_media():
    only_audio = {**MST_STATUS, "media_attachments": [MST_STATUS["media_attachments"][2]]}
    assert MastodonAdapter().normalize([only_audio]) == []
