r"""Windows 長路徑（MAX_PATH）。

## 在修什麼

Windows 的一般路徑上限是 260 字元（**含結尾的 NUL**，所以實際能用的是 259）。
超過的話 `os.path.exists()` 回 `False`、`open()` 丟 `FileNotFoundError` ——
**症狀跟「檔案不存在」一模一樣，沒有任何訊息說是路徑太長。**

正式庫實測（2026-08-21）：`media.local_path` 有 606 筆長度 ≥260，
加上 `\\?\` 前綴之後 **606 筆全部存在**，一筆都沒真的不見。
在此之前 GUI 對這 606 筆一律回 `file.missing` 404 加上「被刪掉，或那顆碟沒插」——
那是**捏造的診斷**，正是根因原則要擋的東西。

## 為什麼不改登錄檔

`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled` 設成 1 也可以解，
但那是機器設定不是專案設定：要管理員權限、要重開機、換一台電腦就沒了
（實測這台是 `0`）。離線盤點媒體庫的掃描腳本早就在程式端自己處理，
掃描器看得到而 backend 看不到，這個不對稱才是 bug 的來源。

## 兩種形狀，不可以混用

    for_io(p)   → 拿去碰磁碟的形狀（`\\?\K:\...`）
    strip(p)    → 給人看、存進 DB 的形狀（`K:\...`）

**`local_path` 永遠存 `strip` 那一種。** 把 `\\?\` 存進 DB 的話：GUI 上會出現
使用者看不懂的前綴、跨機器搬 DB 時前綴變成髒資料、而且
`api/files.py` 的根目錄白名單比對會整個對不上
（`Path(r"\\?\K:\a\b").is_relative_to(Path(r"K:\a"))` 是 `False`）。
安全檢查用一般路徑，只在真的要 I/O 的那一行才 `for_io`。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["for_io", "strip", "is_prefixed"]

# 前綴本身。`\\?\` 是 Win32 的「不要正規化這條路徑」記號 ——
# 它同時關掉 MAX_PATH 檢查與 `.`/`..`/短檔名的展開。
_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"

# ⚠️ 抽成模組常數而不是每次現場問 `os.name`，是為了**讓轉換邏輯在任何平台都測得到**。
# 測試 monkeypatch 這個值成 True，就能在 Linux 上驗證 UNC 與磁碟機代號的轉換規則。
# 直接寫 `os.name != "nt"` 的話，那些規則在非 Windows 的 CI 上是一行都沒跑到的死碼。
_WINDOWS = os.name == "nt"


def is_prefixed(p: Path | str) -> bool:
    return str(p).startswith(_PREFIX)


def for_io(p: Path | str) -> Path:
    r"""回一個「可以拿去碰磁碟」的路徑。

    Windows 以外的平台原樣回傳 —— 這個模組的測試要在任何平台都跑得起來，
    而 CI 與公開版使用者不一定是 Windows。

    ⚠️ **只接受絕對路徑。** `\\?\` 關掉了路徑正規化，相對路徑加上前綴之後
    Win32 不會幫你接上目前工作目錄，得到的是一條指向不存在位置的路徑 ——
    而失敗的樣子又是 `FileNotFoundError`，跟我們正在修的 bug 同一個症狀。
    所以這裡明確丟 `ValueError`，**不**偷偷 `resolve()` 兜過去：呼叫端傳相對
    路徑進來就是它自己有問題，掩蓋掉只會讓下一個人查更久。
    """
    s = str(p)
    if not _WINDOWS:
        return Path(s)
    if s.startswith(_PREFIX):
        return Path(s)
    if not _is_absolute_nt(s):
        raise ValueError(
            f"for_io() 只吃絕對路徑，收到相對路徑：{s!r}。"
            "呼叫端要先自己 resolve()（這裡不代做，見 docstring）。"
        )
    if s.startswith("\\\\"):
        # UNC：\\server\share\... -> \\?\UNC\server\share\...
        return Path(_UNC_PREFIX + s[2:])
    return Path(_PREFIX + s)


def strip(p: Path | str) -> Path:
    r"""把 `\\?\` 前綴拿掉。存進 DB、印給使用者看之前都要走這一支。"""
    s = str(p)
    if s.startswith(_UNC_PREFIX):
        return Path("\\\\" + s[len(_UNC_PREFIX):])
    if s.startswith(_PREFIX):
        return Path(s[len(_PREFIX):])
    return Path(s)


def _is_absolute_nt(s: str) -> bool:
    r"""Windows 意義下的絕對路徑嗎？

    不能用 `Path(s).is_absolute()` —— 在 POSIX 上跑測試時 `PurePosixPath`
    會說 `K:\a\b` 不是絕對路徑，於是這個模組的行為會跟著作業系統漂移。
    這裡只認兩種：磁碟機代號（`K:\`）與 UNC（`\\server\share`）。
    `\a\b` 這種「有根但沒有磁碟機」的路徑**不算**，因為它到底指哪一顆碟
    取決於當下的工作目錄 —— 那正是我們不想要的不確定性。
    """
    if s.startswith("\\\\"):
        return True
    return len(s) >= 3 and s[1] == ":" and s[2] in "\\/"
