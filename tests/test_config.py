"""設定載入。重點在路徑類設定 —— 打錯要當場炸，不要靜默用預設值。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from snsmediadl.config import Config, ensure_output_root, load_config


def write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# ── output_root ───────────────────────────────────────────

def test_output_root_from_toml(tmp_path):
    cfg = load_config(write_toml(tmp_path, "output_root = 'D:/media'"))
    assert cfg.output_root == Path("D:/media")
    assert isinstance(cfg.output_root, Path)


def test_output_root_from_env_beats_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("SNSMEDIADL_OUTPUT_ROOT", str(tmp_path / "env"))
    cfg = load_config(write_toml(tmp_path, "output_root = 'D:/toml'"))
    assert cfg.output_root == tmp_path / "env"


def test_missing_config_file_is_not_an_error(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.output_root.name == "downloads"


def test_unknown_key_is_rejected(tmp_path):
    """設定項打錯字要當場講，不然使用者會以為設定生效了。"""
    with pytest.raises(ValueError, match="output_rot"):
        load_config(write_toml(tmp_path, "output_rot = 'D:/media'"))


# ── extra_media_roots ─────────────────────────────────────

def test_extra_media_roots_from_toml(tmp_path):
    cfg = load_config(write_toml(
        tmp_path, "extra_media_roots = ['D:/old', 'E:/archive']"
    ))
    assert cfg.extra_media_roots == [Path("D:/old"), Path("E:/archive")]
    assert all(isinstance(p, Path) for p in cfg.extra_media_roots)


def test_extra_media_roots_defaults_to_empty(tmp_path):
    assert load_config(tmp_path / "nope.toml").extra_media_roots == []


def test_extra_media_roots_from_env_uses_pathsep(tmp_path, monkeypatch):
    """不能用 ':' 分隔 —— Windows 路徑本身含 ':'，會被切成兩半。"""
    monkeypatch.setenv(
        "SNSMEDIADL_EXTRA_MEDIA_ROOTS", os.pathsep.join(["D:/old", "E:/archive"])
    )
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.extra_media_roots == [Path("D:/old"), Path("E:/archive")]


def test_extra_media_roots_ignores_blank_entries(tmp_path, monkeypatch):
    """尾隨分隔符不該產生一個指向工作目錄的空路徑。"""
    monkeypatch.setenv("SNSMEDIADL_EXTRA_MEDIA_ROOTS", "D:/old" + os.pathsep)
    assert load_config(tmp_path / "nope.toml").extra_media_roots == [Path("D:/old")]


def test_extra_media_roots_rejects_bare_string(tmp_path):
    """toml 寫成字串而不是陣列，會變成一個個字元的路徑清單 —— 要明確擋掉。"""
    with pytest.raises(ValueError, match="路徑清單"):
        load_config(write_toml(tmp_path, "extra_media_roots = 'D:/old'"))


# ── media_roots ───────────────────────────────────────────

def test_media_roots_includes_output_root_first():
    cfg = Config(output_root=Path("D:/new"), extra_media_roots=[Path("D:/old")])
    assert cfg.media_roots == [Path("D:/new"), Path("D:/old")]


def test_media_roots_deduplicates():
    """舊根目錄忘了從清單移掉，不該讓白名單出現重複項。"""
    cfg = Config(output_root=Path("D:/same"), extra_media_roots=[Path("D:/same")])
    assert cfg.media_roots == [Path("D:/same")]


# ── ensure_output_root ────────────────────────────────────

def test_ensure_output_root_creates_missing_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "media"
    assert ensure_output_root(target) == target.resolve()
    assert target.is_dir()


def test_ensure_output_root_leaves_no_probe_file(tmp_path):
    target = tmp_path / "media"
    ensure_output_root(target)
    assert list(target.iterdir()) == []


def test_ensure_output_root_reports_the_path_on_failure(tmp_path):
    """路徑不通時，訊息要指出是哪個路徑 —— 不然使用者不知道去哪改。"""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    target = blocker / "media"

    with pytest.raises(RuntimeError, match="media"):
        ensure_output_root(target)


def test_ensure_output_root_does_not_fall_back(tmp_path):
    """絕不悄悄改用預設目錄：使用者會以為在寫 D 槽，實際堆進專案資料夾。"""
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")

    with pytest.raises(RuntimeError):
        ensure_output_root(blocker / "media")
    assert not (Path(__file__).resolve().parent.parent / "downloads" / "media").exists()
