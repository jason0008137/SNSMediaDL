"""刪除端點。**只刪記錄，不刪本機檔案。**

兩段式：先 `deletion-preview` 看會刪什麼，再帶 `confirm=true` 真的刪。

理由：這是本機、刻意不做認證的服務。一個手滑的 curl 不該能清掉
幾個月的採集記錄。單筆 media 例外（影響範圍就一筆）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..services import deletion
from .app import get_session

router = APIRouter(prefix="/api", tags=["deletion"])


def _require_confirm(confirm: bool, summary: deletion.DeletionSummary, what: str) -> None:
    if confirm:
        return
    detail = (
        f"這會刪掉 {what}：{summary.posts} 則貼文、{summary.media} 筆媒體記錄。"
        " 確認後請帶 confirm=true。"
    )
    if summary.warnings:
        detail += " " + " ".join(summary.warnings)
    raise HTTPException(400, detail)


@router.get("/accounts/{account_id}/deletion-preview")
def preview_account(
    account_id: int, session: Session = Depends(get_session)
) -> dict:
    try:
        return deletion.preview_account_deletion(session, account_id).as_dict()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/accounts/{account_id}")
def remove_account(
    account_id: int,
    confirm: bool = Query(False, description="必須明確帶 true 才會真的刪"),
    session: Session = Depends(get_session),
) -> dict:
    try:
        summary = deletion.preview_account_deletion(session, account_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

    _require_confirm(confirm, summary, f"帳號 {summary.screen_name} 的全部資料")
    return deletion.delete_account(session, account_id).as_dict()


@router.delete("/posts/{post_id}")
def remove_post(
    post_id: int,
    confirm: bool = Query(False),
    session: Session = Depends(get_session),
) -> dict:
    try:
        summary = deletion.preview_post_deletion(session, post_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc

    _require_confirm(confirm, summary, f"貼文 #{post_id}")
    return deletion.delete_post(session, post_id).as_dict()


@router.delete("/media/{media_id}")
def remove_media(media_id: int, session: Session = Depends(get_session)) -> dict:
    """單筆媒體記錄。影響範圍就一筆，所以不要求 confirm。

    檔案一樣留著 —— 這個端點刪的是「DB 認得這個檔」這件事。
    """
    try:
        return deletion.delete_media(session, media_id).as_dict()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
