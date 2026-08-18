"""平台 → 網址（正向建構器）。

**純函式，不碰網路、不碰 DB。**

`urls.py` 是反向的（網址 → 抓取目標）。這一支是正向：拿 DB 裡的欄位拼出
「連回平台」的網址。兩者刻意分開 —— 反向要解析各式各樣的使用者輸入，
正向只需要產生一種正規形式，混在一起只會讓兩邊都難讀。

## 為什麼放後端而不是前端

平台知識已經有兩份在 Python（adapter、`urls.py`）。前端再寫一份 JS 版本，
就變成改一邊忘一邊。API 直接回網址，前端拿到什麼顯示什麼，
**前端不得自行拼接平台網址**。

## 拼不出來時要說為什麼

`(url, problem)` 兩者恰有一個非 None。**絕不猜、絕不 fallback** ——
拼不出來就回問題，不回一個「大概對」的網址。連到錯的地方比 404 更糟：
404 使用者知道要回報，錯的地方使用者只會以為那個作者刪帳號了。
"""

from __future__ import annotations

from .services.identity import is_placeholder

# 平台 → 顯示名。介面上「在 … 開啟」用它。
# Fediverse 的顯示名是 instance host（misskey.io 才是使用者認得的名字，
# 「misskey」是軟體名），所以那兩個平台在 `display_name()` 走 host。
_PLATFORM_LABELS = {
    "x": "X",
    "misskey": "misskey",
    "mastodon": "Mastodon",
    "pixiv": "pixiv",
}

# 用 instance host 組網址的平台。
_HOSTED = {"misskey", "mastodon"}


def display_name(platform: str, host: str | None) -> str:
    """介面上這個平台叫什麼。未知平台回原字串（不猜）。"""
    if platform in _HOSTED and host:
        return host
    return _PLATFORM_LABELS.get(platform, platform)


def _unknown_platform(platform: str) -> str:
    # baraag 是真實案例：migration 之前 `platform` 存的是 instance 名而不是
    # 軟體名。**不可以偷偷當成 mastodon** —— 那會讓沒跑 migration 這件事
    # 永遠不被發現，正是根因原則要防的掩蓋。
    if platform == "baraag":
        return "未知平台 baraag —— 是否還沒跑 migration？（baraag 應該已改成 mastodon）"
    return f"未知平台 {platform} —— 沒有對應的網址規則"


def _need_host(platform: str) -> str:
    return f"缺 instance_host —— {platform} 的網址需要知道是哪個站台"


def _need_screen_name(platform: str) -> str:
    return f"缺 screen_name —— {platform} 的網址用帳號名組成"


def profile_url(
    platform: str,
    host: str | None,
    user_id: str | None,
    screen_name: str | None,
) -> tuple[str | None, str | None]:
    """帳號頁網址。回 `(網址, 問題說明)`，兩者恰有一個非 None。"""
    platform = (platform or "").lower()

    if platform == "x":
        if not screen_name:
            return None, _need_screen_name("X")
        return f"https://x.com/{screen_name}", None

    if platform in _HOSTED:
        if not host:
            return None, _need_host(platform)
        if not screen_name:
            return None, _need_screen_name(platform)
        return f"https://{host}/@{screen_name}", None

    if platform == "pixiv":
        # pixiv 的個人頁只吃數字 id，沒有以名字為準的網址 —— 所以哨符列
        # 是真的拼不出來，不是「懶得拼」。
        if is_placeholder(user_id):
            return None, (
                "這個 pixiv 帳號還沒有真正的 user id（匯入時只有名字）—— "
                "抓取一次會自動治療，之後就連得過去"
            )
        if not user_id:
            return None, "缺 platform_user_id —— pixiv 的個人頁只認數字 id"
        if not user_id.isdigit():
            return None, f"pixiv 的 user id 應該是數字，DB 裡是 {user_id!r}"
        return f"https://www.pixiv.net/users/{user_id}", None

    return None, _unknown_platform(platform)


def post_url(
    platform: str,
    host: str | None,
    post_id: str | None,
    screen_name: str | None,
) -> tuple[str | None, str | None]:
    """單則貼文網址。回 `(網址, 問題說明)`，兩者恰有一個非 None。"""
    platform = (platform or "").lower()

    if not post_id:
        return None, "缺 platform_post_id —— 沒有貼文 id 就連不回去"

    if platform == "x":
        if not screen_name:
            return None, _need_screen_name("X")
        return f"https://x.com/{screen_name}/status/{post_id}", None

    if platform == "misskey":
        # misskey 的貼文網址**不含帳號名**，只要 note id。
        if not host:
            return None, _need_host("misskey")
        return f"https://{host}/notes/{post_id}", None

    if platform == "mastodon":
        # Mastodon 相反 —— 正規網址是 /@user/id。
        if not host:
            return None, _need_host("mastodon")
        if not screen_name:
            return None, _need_screen_name("mastodon")
        return f"https://{host}/@{screen_name}/{post_id}", None

    if platform == "pixiv":
        # 作品頁只認作品 id，與帳號 id 無關 —— 所以 `sn:` 哨符帳號的
        # **貼文**仍然連得過去。這是 profile_url 與 post_url 的真實差異，
        # 不要為了對稱把哨符檢查也複製過來。
        return f"https://www.pixiv.net/artworks/{post_id}", None

    return None, _unknown_platform(platform)
