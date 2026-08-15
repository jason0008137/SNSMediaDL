"""列舉值。

存成 TEXT + CHECK constraint，不用 SQLAlchemy native enum ——
SQLite 沒有原生 enum，用 native enum 之後改值要重建整張表。

Python 3.10 沒有 StrEnum，因此用 (str, Enum) 取得同樣的字串行為。
"""

from __future__ import annotations

from enum import Enum


class _Str(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - 只是讓 f-string 好看
        return self.value

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


class Platform(_Str):
    X = "x"
    MISSKEY = "misskey"
    BARAAG = "baraag"
    PIXIV = "pixiv"


class MediaKind(_Str):
    """媒體型別。掛在 media 不掛 post ——
    實測證實單一貼文可同時含 photo + video + animated_gif。"""

    PHOTO = "photo"
    VIDEO = "video"
    ANIMATED_GIF = "animated_gif"
    # pixiv 的動圖：一個 zip 裝一堆 jpg + 每幀的延遲毫秒數。
    # 第一版只存 zip 不轉檔（轉檔要拉 ffmpeg，是後處理不是採集）。
    UGOIRA = "ugoira"


class MediaStatus(_Str):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"


class Rating(_Str):
    """分級。與 ContentType 正交 —— 「AI 生的 R18」是 ai + r18。

    NULL 代表「未知」，不是 sfw。猜錯的方向不對稱，未知就留空。
    """

    SFW = "sfw"
    R18 = "r18"


class ContentType(_Str):
    ILLUST = "illust"
    IRL = "irl"
    MOD = "mod"
    AI = "ai"
    # 成員名不能以數字開頭，所以是 THREE_D —— **值**才是 "3d"。
    # 存進 DB 與 API 的都是值，成員名只有 Python 這邊看得到。
    THREE_D = "3d"
    PHOTOGRAPH = "photograph"
    OTHER = "other"


class RatingSource(_Str):
    """分級是誰標的。不記來源的話，機器猜的會和人工確認的混在一起。"""

    MANUAL = "manual"
    ACCOUNT_DEFAULT = "account_default"
    AUTO = "auto"


class AccountRole(_Str):
    MAIN = "main"
    ALT = "alt"
    R18_ALT = "r18_alt"


class FetchStatus(_Str):
    """最後一次擷取的結果。

    `OK` 與 `NO_NEW` 刻意分開：兩者都是「成功」，但使用者要知道的是
    「有沒有東西進來」。合成一個 ok 就等於把答案藏起來 —— 而「查了但
    對方沒發新的」正是這個功能要消除的疑惑。
    """

    OK = "ok"                      # 跑完，有新貼文
    NO_NEW = "no_new"              # 跑完，沒有新的（增量立刻停）
    RATE_LIMITED = "rate_limited"  # 429
    NOT_FOUND = "not_found"        # 404 —— 帳號改名或打錯字
    AUTH_REQUIRED = "auth_required"  # 憑證缺失或過期
    FAILED = "failed"              # 其他例外
    SKIPPED = "skipped"            # 該站台已被標記限速，這輪先跳過
