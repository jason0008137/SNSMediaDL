"""刪除記錄。**只刪資料，一個檔案都不碰。**

這是使用者的明確要求，也是這個模組的安全底線：
本模組不 import `os`、不 import `pathlib`，**沒有任何刪檔的能力**。

⚠️ 刪除的真正後果不是 SQL，是這個：

下載 worker 的「不重抓」判斷（`downloader/worker.py` 的 `_already_downloaded`）
靠的是 DB 裡的 `local_path` + `file_hash`。記錄刪掉之後這兩個欄位就沒了，
所以**日後如果重新採集同一個帳號，那些檔案會被重新下載**，
而且因為 `resolve_collision`，會存成 `xxx (1).jpg` 這種副本而不是覆蓋。

不刪帳號就不會發生。但會發生的時候使用者必須事先知道 ——
所以每個刪除函式都回傳「有幾個檔案留在磁碟上」，呼叫端負責講出來。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db.enums import MediaStatus
from ..db.models import Account, Media, Post

log = logging.getLogger("snsmediadl.deletion")


@dataclass
class DeletionSummary:
    """刪了什麼（或：會刪什麼，preview 用同一個型別）。"""

    account_id: int | None = None
    screen_name: str = ""
    platform: str = ""
    posts: int = 0
    media: int = 0
    # 已經下載到磁碟的筆數。**這些檔案會留著**，但 DB 從此不認得它們。
    downloaded_files_kept: int = 0
    # 正在下載中的筆數。刪掉等於打斷它們 —— 使用者該知道。
    interrupted_downloads: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "screen_name": self.screen_name,
            "platform": self.platform,
            "posts": self.posts,
            "media": self.media,
            "downloaded_files_kept": self.downloaded_files_kept,
            "interrupted_downloads": self.interrupted_downloads,
            "warnings": self.warnings,
        }


def _post_ids(account_id: int):
    return select(Post.id).where(Post.account_id == account_id)


def _count_media(session: Session, post_id_stmt) -> tuple[int, int, int]:
    """(總數, 已下載, 下載中)。"""
    total = session.scalar(
        select(func.count()).select_from(Media).where(Media.post_id.in_(post_id_stmt))
    ) or 0
    done = session.scalar(
        select(func.count()).select_from(Media).where(
            Media.post_id.in_(post_id_stmt),
            Media.status == MediaStatus.DONE.value,
            Media.local_path.is_not(None),
        )
    ) or 0
    downloading = session.scalar(
        select(func.count()).select_from(Media).where(
            Media.post_id.in_(post_id_stmt),
            Media.status == MediaStatus.DOWNLOADING.value,
        )
    ) or 0
    return total, done, downloading


def _warnings(summary: DeletionSummary) -> list[str]:
    out: list[str] = []
    if summary.downloaded_files_kept:
        out.append(
            f"{summary.downloaded_files_kept} 個已下載的檔案會留在磁碟上，"
            "但 DB 從此不認得它們 —— 日後若重新採集同一個帳號，"
            "這些檔案會被重新下載成副本（xxx (1).jpg），不會覆蓋原檔。"
        )
    if summary.interrupted_downloads:
        out.append(
            f"其中 {summary.interrupted_downloads} 筆正在下載中，刪除會打斷它們。"
        )
    return out


def preview_account_deletion(session: Session, account_id: int) -> DeletionSummary:
    """會刪掉什麼。**不做任何寫入。**

    刪除是不可逆的（檔案雖然還在，記錄沒了就是沒了），
    所以呼叫端要先讓使用者看到數字再決定。
    """
    account = session.get(Account, account_id)
    if account is None:
        raise LookupError(f"找不到 account#{account_id}")

    stmt = _post_ids(account_id)
    posts = session.scalar(
        select(func.count()).select_from(Post).where(Post.account_id == account_id)
    ) or 0
    media, done, downloading = _count_media(session, stmt)

    summary = DeletionSummary(
        account_id=account_id,
        screen_name=account.screen_name or account.platform_user_id,
        platform=account.platform,
        posts=posts,
        media=media,
        downloaded_files_kept=done,
        interrupted_downloads=downloading,
    )
    summary.warnings = _warnings(summary)
    return summary


def delete_account(session: Session, account_id: int) -> DeletionSummary:
    """刪掉一個帳號連同它全部的貼文與媒體**記錄**。檔案不動。

    ⚠️ **不要改成 `session.delete(account)`。** `Account.posts` 這個
    relationship 沒有設 cascade，ORM 刪父物件時的預設行為是把子物件的 FK
    設成 NULL —— 而 `posts.account_id` 是 NOT NULL，會直接炸。
    DB 層雖然有 `ondelete="CASCADE"`，但 ORM 會搶在資料庫之前先 nullify。

    所以用 Core 的 `delete()` 由下往上明確刪。多三行，但行為是看得見的。

    `creators` 不動：一位創作者可以有多個帳號（本帳 + 小帳），
    刪掉一個帳號不代表這個人不存在了。
    """
    summary = preview_account_deletion(session, account_id)

    post_ids = _post_ids(account_id)
    session.execute(delete(Media).where(Media.post_id.in_(post_ids)))
    session.execute(delete(Post).where(Post.account_id == account_id))
    session.execute(delete(Account).where(Account.id == account_id))
    session.commit()

    log.warning(
        "已刪除 account#%s（%s@%s）：%s 則貼文 / %s 筆媒體記錄。"
        "%s 個檔案留在磁碟上，DB 不再認得它們。",
        account_id, summary.screen_name, summary.platform,
        summary.posts, summary.media, summary.downloaded_files_kept,
    )
    return summary


def preview_post_deletion(session: Session, post_id: int) -> DeletionSummary:
    post = session.get(Post, post_id)
    if post is None:
        raise LookupError(f"找不到 post#{post_id}")

    stmt = select(Post.id).where(Post.id == post_id)
    media, done, downloading = _count_media(session, stmt)

    summary = DeletionSummary(
        account_id=post.account_id,
        platform=post.platform,
        posts=1,
        media=media,
        downloaded_files_kept=done,
        interrupted_downloads=downloading,
    )
    summary.warnings = _warnings(summary)
    return summary


def delete_post(session: Session, post_id: int) -> DeletionSummary:
    """刪一則貼文與它的媒體記錄。帳號留著。"""
    summary = preview_post_deletion(session, post_id)

    session.execute(delete(Media).where(Media.post_id == post_id))
    session.execute(delete(Post).where(Post.id == post_id))
    session.commit()

    log.warning(
        "已刪除 post#%s：%s 筆媒體記錄，%s 個檔案留在磁碟上。",
        post_id, summary.media, summary.downloaded_files_kept,
    )
    return summary


def delete_media(session: Session, media_id: int) -> DeletionSummary:
    """刪一筆媒體記錄。貼文與檔案都留著。"""
    media = session.get(Media, media_id)
    if media is None:
        raise LookupError(f"找不到 media#{media_id}")

    summary = DeletionSummary(
        media=1,
        downloaded_files_kept=int(
            media.status == MediaStatus.DONE.value and bool(media.local_path)
        ),
        interrupted_downloads=int(media.status == MediaStatus.DOWNLOADING.value),
    )
    summary.warnings = _warnings(summary)

    session.execute(delete(Media).where(Media.id == media_id))
    session.commit()
    return summary
