"""個人偏好：五星評分與我的最愛。

**刻意不併進 `tagging.py`。** 那支管的是分級標記，有一整套繼承／回溯語意：
帳號預設值 → ingest 時繼承給新貼文 → `retag` 回溯 → `rating_source` 記錄
是誰標的。這裡的 `stars` / `is_favorite` **一項都沒有** —— 不繼承、不回溯、
沒有來源概念。放在一起會讓人以為帳號的 `stars` 會流到貼文上。

⚠️ `stars`（五星評分）與 `rating`（sfw / r18 分級）是**兩件不同的事**，
兩者正交：一張圖可以既是 r18 又是五星。分級走 `tagging.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Account, Media
from .app import get_session

router = APIRouter(prefix="/api", tags=["prefs"])


class StarsIn(BaseModel):
    # NULL = 清除評分。**不是** 0 分 —— 0 會被 CHECK 擋下。
    stars: int | None = None


class BulkStarsIn(BaseModel):
    media_ids: list[int]
    stars: int | None = None


class AccountPrefsIn(BaseModel):
    stars: int | None = None
    is_favorite: bool | None = None


def _validate_stars(stars: int | None) -> None:
    """值域集中在一處。DB 的 CHECK 是最後一道防線，不是錯誤訊息的來源 ——
    讓它擋下來的話使用者看到的是 500 + SQLite 的英文約束名。"""
    if stars is not None and not 1 <= stars <= 5:
        raise HTTPException(422, "stars 必須是 1–5，清除評分請送 null（不是 0）")


@router.patch("/media/{media_id}/stars")
def set_media_stars(
    media_id: int, body: StarsIn, session: Session = Depends(get_session)
) -> dict:
    media = session.get(Media, media_id)
    if media is None:
        raise HTTPException(404, "media not found")
    _validate_stars(body.stars)
    media.stars = body.stars
    session.commit()
    return {"id": media.id, "stars": media.stars}


@router.post("/media/bulk-stars")
def bulk_set_stars(body: BulkStarsIn, session: Session = Depends(get_session)) -> dict:
    """批次打星。選取模式用。

    與 `tagging.py::bulk_set_tags` 的差別值得記一筆：那支必須先把 media 去重成
    post 清單再送（分級掛在 post，選了同一則的四張圖只該算一則）。
    **打星不用** —— `stars` 就掛在 media 上，選幾個就是改幾個。
    """
    _validate_stars(body.stars)
    if not body.media_ids:
        return {"updated": 0, "requested": 0}

    rows = session.scalars(select(Media).where(Media.id.in_(body.media_ids))).all()
    for media in rows:
        media.stars = body.stars
    session.commit()
    return {"updated": len(rows), "requested": len(set(body.media_ids))}


@router.patch("/accounts/{account_id}/prefs")
def set_account_prefs(
    account_id: int, body: AccountPrefsIn, session: Session = Depends(get_session)
) -> dict:
    account = session.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    _validate_stars(body.stars)

    # 用 model_fields_set 區分「沒帶這個欄位」與「明確帶 null」——
    # 後者是使用者把星星點掉，必須真的清成 NULL。只判斷 `is not None`
    # 的話清除評分會變成無聲的 no-op。
    provided = body.model_fields_set
    if "stars" in provided:
        account.stars = body.stars
    if "is_favorite" in provided and body.is_favorite is not None:
        account.is_favorite = body.is_favorite
    session.commit()

    return {
        "id": account.id,
        "stars": account.stars,
        "is_favorite": account.is_favorite,
    }
