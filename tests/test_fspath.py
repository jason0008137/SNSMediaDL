r"""`snsmediadl/fspath.py` —— Windows 長路徑前綴。

轉換規則的測試都 monkeypatch `_WINDOWS = True`，這樣在 Linux 上也跑得到；
真的碰磁碟的那兩個才 skip 非 Windows。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from snsmediadl import fspath

WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="真的碰 Windows 檔案系統")


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(fspath, "_WINDOWS", True)


# ────────────────────────────────── for_io

def test_drive_letter_gets_prefix(as_windows):
    assert str(fspath.for_io(r"K:\_Twitter_Pack\a.jpg")) == r"\\?\K:\_Twitter_Pack\a.jpg"


def test_already_prefixed_is_untouched(as_windows):
    r"""冪等。中途多包一次不該變成 `\\?\\\?\...`。"""
    once = fspath.for_io(r"K:\a\b.jpg")
    assert fspath.for_io(once) == once


def test_unc_uses_the_unc_form(as_windows):
    r"""`\\server\share` 要變 `\\?\UNC\server\share`，不是 `\\?\\\server\share`。"""
    assert str(fspath.for_io(r"\\nas\media\a.jpg")) == r"\\?\UNC\nas\media\a.jpg"


def test_relative_path_is_refused(as_windows):
    """相對路徑加前綴會得到一條指向不存在位置的路徑，而症狀跟本來的 bug 一樣。"""
    with pytest.raises(ValueError, match="絕對路徑"):
        fspath.for_io(r"downloads\x\a.jpg")


def test_rooted_but_driveless_is_refused(as_windows):
    r"""`\a\b` 指哪顆碟要看當下的工作目錄 —— 那正是不能默默接受的不確定性。"""
    with pytest.raises(ValueError, match="絕對路徑"):
        fspath.for_io(r"\_Twitter_Pack\a.jpg")


def test_forward_slash_drive_is_absolute(as_windows):
    """`K:/a/b` 也是絕對路徑。sqlite 裡混著這種寫法，不能被當成相對路徑擋掉。"""
    assert str(fspath.for_io("K:/a/b.jpg")).startswith("\\\\?\\K:")


def test_non_windows_is_a_passthrough(monkeypatch):
    monkeypatch.setattr(fspath, "_WINDOWS", False)
    assert fspath.for_io("/mnt/k/a.jpg") == Path("/mnt/k/a.jpg")


def test_accepts_path_objects(as_windows):
    assert str(fspath.for_io(Path(r"K:\a\b.jpg"))) == r"\\?\K:\a\b.jpg"


# ────────────────────────────────── strip

def test_strip_removes_prefix():
    assert str(fspath.strip(r"\\?\K:\a\b.jpg")) == r"K:\a\b.jpg"


def test_strip_restores_unc():
    assert str(fspath.strip(r"\\?\UNC\nas\media\a.jpg")) == r"\\nas\media\a.jpg"


def test_strip_is_a_noop_on_plain_paths():
    assert str(fspath.strip(r"K:\a\b.jpg")) == r"K:\a\b.jpg"


def test_round_trip(as_windows):
    """存進 DB 的形狀要能原封不動回來 —— 不然重跑會生出第二筆路徑。"""
    for raw in (r"K:\a\b.jpg", r"\\nas\media\a.jpg"):
        assert str(fspath.strip(fspath.for_io(raw))) == raw


def test_is_prefixed():
    assert fspath.is_prefixed(r"\\?\K:\a")
    assert not fspath.is_prefixed(r"K:\a")


# ────────────────────────────────── 真的碰磁碟

@WINDOWS_ONLY
def test_long_path_is_reachable_only_with_the_prefix(tmp_path):
    """這一項才是整個模組存在的理由：>260 的路徑，沒有前綴就「不存在」。"""
    deep = tmp_path
    while len(str(deep)) < 250:
        deep = deep / ("d" * 40)
    # ⚠️ 連建目錄本身都得走前綴 —— `deep.mkdir(parents=True)` 會在墊到第四層時
    #    丟 WinError 206（檔名或副檔名太長），那就是同一個 MAX_PATH。
    fspath.for_io(deep).mkdir(parents=True)
    target = deep / ("n" * 40 + ".bin")
    assert len(str(target)) > 260, f"沒墊夠長：{len(str(target))}"

    fspath.for_io(target).write_bytes(b"hello")

    assert not target.exists(), "這台機器的 MAX_PATH 沒生效，這個測試就驗不到東西"
    assert fspath.for_io(target).exists()
    assert fspath.for_io(target).read_bytes() == b"hello"


@WINDOWS_ONLY
def test_short_path_behaves_the_same_either_way(tmp_path):
    """短路徑加了前綴也要照常運作 —— 不然就得在呼叫端判斷長度，那遲早會漏。"""
    p = tmp_path / "a.bin"
    p.write_bytes(b"x")
    assert fspath.for_io(p).exists()
    assert fspath.for_io(p).stat().st_size == 1
