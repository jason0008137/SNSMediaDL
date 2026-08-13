"""網址 → 抓取目標。

**純函式，不碰網路。**

⚠️ 為什麼不自動探測平台（打 `/api/meta` 判斷 misskey、`/api/v1/instance`
判斷 mastodon）：那會在「解析一行文字」這件事上引入網路請求與逾時，
而且失敗模式很糟 —— 猜錯平台就用錯 parser，症狀是抓不到東西而不是報錯。
支援的 host 就那幾個，用明表；不認得就明講，並提供 `平台|網址` 的覆寫。

新增一個 instance = 在 `KNOWN_HOSTS` 加一列，不改任何邏輯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# host -> backend 的 platform 名
KNOWN_HOSTS: dict[str, str] = {
    "misskey.io": "misskey",
    "baraag.net": "mastodon",
    "pixiv.net": "pixiv",
    "www.pixiv.net": "pixiv",
}

# 認得但明確不支援。錯誤訊息一定要講替代方案 ——
# 「不支援」而不說怎麼辦，使用者只會再貼一次。
UNSUPPORTED_HOSTS: dict[str, str] = {
    "x.com": "X 不能由 backend 抓（公開 API 已關閉），請用 Chrome extension 採集",
    "twitter.com": "X 不能由 backend 抓（公開 API 已關閉），請用 Chrome extension 採集",
    "mobile.twitter.com": "X 不能由 backend 抓（公開 API 已關閉），請用 Chrome extension 採集",
    "instagram.com": "instagram 還沒做 —— 抓取路徑尚未 recon，目前沒有可用的 adapter",
    "www.instagram.com": "instagram 還沒做 —— 抓取路徑尚未 recon，目前沒有可用的 adapter",
}

# 用 @handle 當身分的平台（Fediverse）。pixiv 用數字 id。
_FEDIVERSE = {"misskey", "mastodon"}

# 裸帳號：@name@host 或 name@host
_BARE_ACCT = re.compile(
    r"^@?(?P<name>[A-Za-z0-9_][A-Za-z0-9_.\-]*)@(?P<host>[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,})$"
)

# 覆寫語法：`平台|網址`。左邊必須是單純的平台名，否則 https://… 的冒號會誤判
_OVERRIDE = re.compile(r"^(?P<platform>[A-Za-z][A-Za-z0-9_]*)\|(?P<rest>.+)$")


class ParseError(ValueError):
    """這一行看不懂或不支援。**訊息會直接顯示給使用者，要寫得可行動。**"""


@dataclass(frozen=True)
class Target:
    """一個可以交給 `services/fetch.py` 的抓取目標。"""

    platform: str
    # 單一站台的平台（pixiv）留空字串。
    # ⚠️ 不可以用 None —— 全鏈路的唯一鍵都用空字串表示「沒有 instance」。
    host: str
    acct: str

    @property
    def key(self) -> tuple[str, str, str]:
        """去重用。帳號名大小寫不敏感（平台自己也是）。"""
        return (self.platform, self.host, self.acct.lower())

    @property
    def label(self) -> str:
        return f"@{self.acct}@{self.host}" if self.host else f"{self.platform}:{self.acct}"


@dataclass
class ParsedLine:
    """一行的解析結果。錯誤不丟例外，因為批次要一次看完全部。"""

    raw: str
    target: Target | None = None
    error: str | None = None
    # 這一批裡前面已經出現過同一個目標
    duplicate: bool = False


def _split_host(netloc: str) -> str:
    """去掉 user:pass@ 與 :port，回小寫 host。"""
    host = netloc.rsplit("@", 1)[-1]
    if host.startswith("["):          # IPv6
        return host.lower()
    return host.split(":", 1)[0].lower()


def _fediverse_acct(platform: str, host: str, segments: list[str]) -> str:
    if not segments:
        raise ParseError(f"{host} 的網址裡沒有帳號 —— 要像 https://{host}/@someone")

    first = segments[0]
    if not first.startswith("@"):
        # misskey 也有 /users/<id> 這種正規網址，但 adapter 是用 username 查的
        raise ParseError(
            f"看不懂 {host} 的這個路徑（{'/'.join(segments)}）—— "
            f"請用 @帳號 形式的網址，例如 https://{host}/@someone"
        )

    name = first[1:]
    if not name:
        raise ParseError(f"{host} 的網址裡只有一個 @，沒有帳號名")

    if "@" in name:
        # /@someone@other.host = 從這個站看到的**遠端使用者**。
        # 刻意不支援：抓下來會掛在 `host` 這個 instance 底下，
        # 同一個人從兩個站抓就會變成兩筆帳號（唯一鍵含 instance_host）。
        remote = name.split("@", 1)[1]
        raise ParseError(
            f"這是從 {host} 看到的遠端使用者（{name}）—— "
            f"請直接用他本站的網址，例如 https://{remote}/@{name.split('@', 1)[0]}"
        )
    return name


def _pixiv_acct(segments: list[str], query: str) -> str:
    # /users/12345 或 /en/users/12345
    for i, seg in enumerate(segments):
        if seg == "users" and i + 1 < len(segments):
            candidate = segments[i + 1]
            if candidate.isdigit():
                return candidate
            raise ParseError(f"pixiv 的 user id 應該是數字，看到 {candidate!r}")

    # 舊版網址 /member.php?id=12345
    if any(seg == "member.php" for seg in segments):
        ids = parse_qs(query).get("id") or []
        if ids and ids[0].isdigit():
            return ids[0]
        raise ParseError("pixiv 的 member.php 網址缺少數字 id")

    if any(seg in ("artworks", "i") for seg in segments):
        raise ParseError(
            "這是單一作品的網址，不是作者頁 —— 請用 https://www.pixiv.net/users/<數字id>"
        )

    raise ParseError(
        "看不懂這個 pixiv 網址 —— 請用作者頁 https://www.pixiv.net/users/<數字id>"
    )


def parse_target(line: str) -> Target:
    """解析一行。看不懂就丟 `ParseError`，**絕不猜**。"""
    text = line.strip()
    if not text:
        raise ParseError("空白行")

    forced: str | None = None
    m = _OVERRIDE.match(text)
    if m:
        forced = m.group("platform").lower()
        text = m.group("rest").strip()
        if forced not in KNOWN_HOSTS.values():
            raise ParseError(
                f"不認得平台 {forced!r}（可用：{sorted(set(KNOWN_HOSTS.values()))}）"
            )

    # 裸帳號 @name@host（沒有 scheme 也沒有路徑）
    bare = _BARE_ACCT.match(text)
    if bare:
        host = bare.group("host").lower()
        platform = forced or KNOWN_HOSTS.get(host)
        if platform is None:
            raise ParseError(_unknown_host_message(host))
        if platform not in _FEDIVERSE:
            raise ParseError(f"{platform} 不吃 @帳號 形式，請用網址")
        return Target(platform=platform, host=host, acct=bare.group("name"))

    if "://" not in text:
        text = f"https://{text}"

    parts = urlsplit(text)
    host = _split_host(parts.netloc)
    if not host:
        raise ParseError("這一行看起來不是網址，也不是 @帳號@站台")

    platform = forced or KNOWN_HOSTS.get(host)
    if platform is None:
        raise ParseError(_unknown_host_message(host))

    segments = [s for s in parts.path.split("/") if s]

    if platform == "pixiv":
        return Target(platform="pixiv", host="", acct=_pixiv_acct(segments, parts.query))

    return Target(
        platform=platform, host=host, acct=_fediverse_acct(platform, host, segments)
    )


def _unknown_host_message(host: str) -> str:
    known = UNSUPPORTED_HOSTS.get(host)
    if known:
        return known
    return (
        f"不認得 {host} —— 支援的站台：{', '.join(sorted(KNOWN_HOSTS))}。"
        f"其他 Fediverse 站台可以用 `misskey|https://{host}/@someone` 指定平台"
    )


def parse_lines(text: str) -> list[ParsedLine]:
    """解析多行。**空白行與 `#` 開頭的註解直接略過，不算錯誤。**

    每一行各自成敗 —— 一行看不懂不影響其他行，這是批次的重點。
    """
    out: list[ParsedLine] = []
    seen: set[tuple[str, str, str]] = set()

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            target = parse_target(stripped)
        except ParseError as exc:
            out.append(ParsedLine(raw=stripped, error=str(exc)))
            continue

        duplicate = target.key in seen
        seen.add(target.key)
        out.append(ParsedLine(raw=stripped, target=target, duplicate=duplicate))

    return out
