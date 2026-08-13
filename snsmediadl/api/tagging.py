"""分級標記。

**帳號預設值改了不回溯既有貼文** —— 歷史標記是事實紀錄。
要回溯走 `/api/accounts/{id}/retag`，而且必須顯式表態要不要蓋掉人工標記。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.enums import ContentType, Rating, RatingSource
from ..db.models import Account, Post
from .app import get_session

router = APIRouter(prefix="/api", tags=["tagging"])


class TagsIn(BaseModel):
    rating: str | None = None
    content_type: str | None = None


class AccountDefaultsIn(BaseModel):
    default_rating: str | None = None
    default_content_type: str | None = None


class BulkTagsIn(BaseModel):
    post_ids: list[int]
    rating: str | None = None
    content_type: str | None = None


class RetagIn(BaseModel):
    # 預設 False：不覆蓋人工標記。要蓋掉必須明講。
    overwrite_manual: bool = False


def _validate(rating: str | None, content_type: str | None) -> None:
    if rating is not None and rating not in Rating.values():
        raise HTTPException(422, f"rating 必須是 {Rating.values()} 之一")
    if content_type is not None and content_type not in ContentType.values():
        raise HTTPException(422, f"content_type 必須是 {ContentType.values()} 之一")


@router.patch("/posts/{post_id}/tags")
def set_post_tags(
    post_id: int, body: TagsIn, session: Session = Depends(get_session)
) -> dict:
    post = session.get(Post, post_id)
    if post is None:
        raise HTTPException(404, "post not found")
    _validate(body.rating, body.content_type)

    # 用 model_fields_set 區分「沒帶這個欄位」與「明確帶 null」——
    # 後者是使用者把下拉選回「未標」，必須真的清掉。
    provided = body.model_fields_set
    if "rating" in provided:
        post.rating = body.rating
    if "content_type" in provided:
        post.content_type = body.content_type
    # 人手動碰過就是 manual，之後的批次重標預設不會蓋掉它
    post.rating_source = RatingSource.MANUAL.value
    session.commit()

    return {
        "id": post.id,
        "rating": post.rating,
        "content_type": post.content_type,
        "rating_source": post.rating_source,
    }


@router.post("/posts/bulk-tags")
def bulk_set_tags(body: BulkTagsIn, session: Session = Depends(get_session)) -> dict:
    """批次標記。GUI 的多選功能用。

    分級掛在 post 不掛 media，所以前端選了同一則貼文的多個媒體時，
    要先去重成 post 清單再送 —— 並在畫面上講清楚影響幾則貼文，
    否則使用者會以為自己只改了一張圖。
    """
    _validate(body.rating, body.content_type)
    if not body.post_ids:
        return {"updated": 0}

    provided = body.model_fields_set
    posts = session.scalars(select(Post).where(Post.id.in_(body.post_ids))).all()

    for post in posts:
        if "rating" in provided:
            post.rating = body.rating
        if "content_type" in provided:
            post.content_type = body.content_type
        post.rating_source = RatingSource.MANUAL.value

    session.commit()
    return {"updated": len(posts), "requested": len(set(body.post_ids))}


@router.patch("/accounts/{account_id}/defaults")
def set_account_defaults(
    account_id: int, body: AccountDefaultsIn, session: Session = Depends(get_session)
) -> dict:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    _validate(body.default_rating, body.default_content_type)

    if body.default_rating is not None:
        account.default_rating = body.default_rating
    if body.default_content_type is not None:
        account.default_content_type = body.default_content_type
    session.commit()

    return {
        "id": account.id,
        "default_rating": account.default_rating,
        "default_content_type": account.default_content_type,
        "note": "既有貼文不會被回溯；要回溯請呼叫 /api/accounts/{id}/retag",
    }


@router.post("/accounts/{account_id}/retag")
def retag_account_posts(
    account_id: int, body: RetagIn, session: Session = Depends(get_session)
) -> dict:
    """用帳號預設值批次重標該帳號的貼文。"""
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    if not account.default_rating and not account.default_content_type:
        raise HTTPException(422, "帳號沒有設定預設值，無從重標")

    stmt = select(Post).where(Post.account_id == account_id)
    if not body.overwrite_manual:
        stmt = stmt.where(
            (Post.rating_source.is_(None))
            | (Post.rating_source != RatingSource.MANUAL.value)
        )

    updated = 0
    for post in session.scalars(stmt):
        if account.default_rating:
            post.rating = account.default_rating
        if account.default_content_type:
            post.content_type = account.default_content_type
        post.rating_source = RatingSource.ACCOUNT_DEFAULT.value
        updated += 1

    session.commit()
    return {"account_id": account_id, "updated": updated,
            "overwrite_manual": body.overwrite_manual}
