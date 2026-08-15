"""分級標記。

**帳號預設值改了不回溯既有貼文** —— 歷史標記是事實紀錄。
要回溯走 `/api/accounts/{id}/retag`，而且必須顯式表態要不要蓋掉人工標記。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import case, func, select
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


class DeriveIn(BaseModel):
    # 預設不覆蓋既有值。已經設過的帳號多半是人工設的，不該被推導結果蓋掉。
    overwrite: bool = False
    dry_run: bool = False
    account_id: int | None = None


@router.post("/accounts/derive-defaults")
def derive_account_defaults(
    body: DeriveIn, session: Session = Depends(get_session)
) -> dict:
    """從帳號**自己的貼文**推導帳號預設值。

    與 `/accounts/{id}/retag` 方向相反：那支是「帳號預設值 → 貼文」，
    這支是「貼文 → 帳號預設值」。舊資料匯入之後貼文都標好了，但帳號層
    全是空的 —— 而帳號層才是瀏覽時真正在用的篩選維度。

    ⚠️ **分級只要出現過一次 r18 就標 r18**，不看比例。

    這是刻意選的方向。`default_rating` 會被之後 ingest 的新貼文繼承：
    把一個「99% sfw 但偶爾畫 r18」的作者標成 sfw，代價是他的新 r18 作品
    在工作安全模式下直接出現在畫面上；反過來標成 r18 的代價只是被多藏起來，
    看得見也改得回來。猜錯的方向不對稱，就往安全那邊倒。

    實測資料：4,146 個帳號全 sfw、463 個全 r18，只有 31 個混合 ——
    這條規則實際影響的是那 31 個。

    類型取「出現最多次的那一種」，因為它沒有安全上的不對稱。
    """
    rating_expr = func.max(
        case((Post.rating == Rating.R18.value, 1), else_=0)
    )
    stmt = (
        select(
            Post.account_id,
            rating_expr.label("has_r18"),
            func.max(case((Post.rating.is_not(None), 1), else_=0)).label("has_rating"),
        )
        .group_by(Post.account_id)
    )
    if body.account_id is not None:
        stmt = stmt.where(Post.account_id == body.account_id)

    ratings = {
        aid: (Rating.R18.value if has_r18 else Rating.SFW.value)
        for aid, has_r18, has_rating in session.execute(stmt)
        if has_rating
    }

    # 類型取眾數。一次查完，不做 N+1。
    ct_stmt = (
        select(Post.account_id, Post.content_type, func.count().label("n"))
        .where(Post.content_type.is_not(None))
        .group_by(Post.account_id, Post.content_type)
        .order_by(Post.account_id, func.count().desc())
    )
    if body.account_id is not None:
        ct_stmt = ct_stmt.where(Post.account_id == body.account_id)
    content: dict[int, str] = {}
    for aid, ctype, _n in session.execute(ct_stmt):
        content.setdefault(aid, ctype)      # 已依次數倒序，第一個就是眾數

    acc_stmt = select(Account)
    if body.account_id is not None:
        acc_stmt = acc_stmt.where(Account.id == body.account_id)

    changed = {"rating": 0, "content_type": 0}
    mixed: list[str] = []
    for acc in session.scalars(acc_stmt):
        r, c = ratings.get(acc.id), content.get(acc.id)
        if r and (body.overwrite or acc.default_rating is None):
            if acc.default_rating != r:
                acc.default_rating = r
                changed["rating"] += 1
        if c and (body.overwrite or acc.default_content_type is None):
            if acc.default_content_type != c:
                acc.default_content_type = c
                changed["content_type"] += 1

    # 哪些帳號其實是混合的 —— 使用者可能想手動覆寫那幾個
    mixed_stmt = (
        select(Account.screen_name)
        .join(Post, Post.account_id == Account.id)
        .group_by(Account.id)
        .having(
            func.max(case((Post.rating == Rating.R18.value, 1), else_=0)) == 1,
            func.max(case((Post.rating == Rating.SFW.value, 1), else_=0)) == 1,
        )
    )
    mixed = [n for (n,) in session.execute(mixed_stmt) if n]

    if body.dry_run:
        session.rollback()
    else:
        session.commit()

    return {
        "dry_run": body.dry_run,
        "updated": changed,
        "mixed_accounts": {
            "n": len(mixed),
            "note": "這些帳號同時有 sfw 與 r18 貼文，一律標成 r18（見端點說明）",
            "sample": mixed[:20],
        },
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
