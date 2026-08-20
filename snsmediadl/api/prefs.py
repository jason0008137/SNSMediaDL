"""個人偏好：五星評分與我的最愛。

**刻意不併進 `tagging.py`。** 那支管的是分級標記，有一整套繼承／回溯語意：
帳號預設值 → ingest 時繼承給新貼文 → `retag` 回溯 → `rating_source` 記錄
是誰標的。這裡的 `stars` / `is_favorite` **一項都沒有** —— 不繼承、不回溯、
沒有來源概念。放在一起會讓人以為帳號的 `stars` 會流到貼文上。

⚠️ `stars`（五星評分）與 `rating`（sfw / r18 分級）是**兩件不同的事**，
兩者正交：一張圖可以既是 r18 又是五星。分級走 `tagging.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.enums import ContentType, Rating
from ..db.models import Account, Media
from .app import get_session
from .errors import ApiError

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
    # 恢復追蹤（自動退訂之後的反悔路）。**必須連 streak 一起歸零** ——
    # 只把 is_tracked 打開的話，下一次找不到就是第 3 次，立刻又被退訂，
    # 使用者會覺得「恢復追蹤」這個按鈕根本沒有用。
    is_tracked: bool | None = None
    # 一鍵更新排除。⚠️ **與 is_tracked 各走各的，不互相牽動** ——
    # 見 `db/models.py::Account.is_ignored`。設為忽略時**不要**順手關掉
    # is_tracked：綁在一起就再也分不出「使用者標的」與「系統退訂的」。
    is_ignored: bool | None = None


def _validate_stars(stars: int | None) -> None:
    """值域集中在一處。DB 的 CHECK 是最後一道防線，不是錯誤訊息的來源 ——
    讓它擋下來的話使用者看到的是 500 + SQLite 的英文約束名。"""
    if stars is not None and not 1 <= stars <= 5:
        raise ApiError(
            "prefs.bad_stars",
            "stars must be 1-5; send null to clear it (not 0).")


@router.patch("/media/{media_id}/stars")
def set_media_stars(
    media_id: int, body: StarsIn, session: Session = Depends(get_session)
) -> dict:
    media = session.get(Media, media_id)
    if media is None:
        raise ApiError("media.not_found", "No such media.", 404)
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
        raise ApiError("account.not_found", "No such account.", 404)
    _validate_stars(body.stars)

    # 用 model_fields_set 區分「沒帶這個欄位」與「明確帶 null」——
    # 後者是使用者把星星點掉，必須真的清成 NULL。只判斷 `is not None`
    # 的話清除評分會變成無聲的 no-op。
    provided = body.model_fields_set
    if "stars" in provided:
        account.stars = body.stars
    if "is_favorite" in provided and body.is_favorite is not None:
        account.is_favorite = body.is_favorite
    if "is_tracked" in provided and body.is_tracked is not None:
        account.is_tracked = body.is_tracked
        if body.is_tracked:
            # 恢復追蹤 = 使用者說「這個帳號是好的」。連續計數必須跟著歸零，
            # 否則下一次找不到就達標，按鈕等於沒按。
            account.not_found_streak = 0
            account.last_fetch_note = None
    if "is_ignored" in provided and body.is_ignored is not None:
        # ⚠️ 這裡**刻意什麼都不連帶做**：不動 is_tracked、不動 streak、
        # 不動任何資料。忽略只是一個旗標。
        account.is_ignored = body.is_ignored
    session.commit()

    return {
        "id": account.id,
        "stars": account.stars,
        "is_favorite": account.is_favorite,
        "is_tracked": account.is_tracked,
        "is_ignored": account.is_ignored,
        "not_found_streak": account.not_found_streak,
    }


# ── 批次 ─────────────────────────────────────────────────
#
# ⚠️ 這一整段最重要的約束：**SQLite 一次繫結變數上限 999**。
# 一次 `WHERE id IN (...)` 塞 4,653 個直接丟 OperationalError，
# 而使用者看到的是「批次失敗」四個字 —— 那時他已經按過確認了，
# 還不知道有沒有改到一半。所以超過就 422，讓呼叫端自己分批。


# 「把這個欄位設成 NULL」的哨符。
#
# ⚠️ 不可以用 `null` 表示這件事：`null` 已經是「這個欄位不動」的意思
# （批次一次可以只改其中幾個欄位）。同一個值表示兩種意思，就會做出
# 「想清空卻清不掉」或「不想動卻被清掉」的 bug。
# 既有的媒體批次已經用這個字串，沿用它，不要發明第二種。
CLEAR = "__clear__"

# 與 `api/query.py::BULK_ID_LIMIT` 同一個數字。故意各自寫一份常數而不是
# 互相 import —— 這兩個模組目前沒有相依關係，為一個整數建立它不划算。
# 兩邊的測試都會釘住 900 這個值。
BULK_ID_LIMIT = 900


class BulkAccountPrefsIn(BaseModel):
    ids: list[int]
    # 三個布林旗標。`None` = 這個欄位不動。
    is_ignored: bool | None = None
    is_tracked: bool | None = None
    is_favorite: bool | None = None
    # 這三個吃 `CLEAR` 哨符（設成 NULL）。`None` 仍然是「不動」。
    stars: int | str | None = None
    default_rating: str | None = None
    default_content_type: str | None = None


def _bulk_value(raw: str | int | None) -> tuple[bool, object]:
    """把批次欄位的值翻成 (要不要改, 改成什麼)。"""
    if raw is None:
        return False, None
    if raw == CLEAR:
        return True, None
    return True, raw


@router.post("/accounts/bulk-prefs")
def bulk_account_prefs(
    body: BulkAccountPrefsIn, session: Session = Depends(get_session)
) -> dict:
    """批次改帳號偏好。

    `missing` **一定要回**：使用者選了 4,653 個而只改到 4,650 個時，
    那 3 個去哪了必須講得出來 —— 不回就是靜默漏掉，而他已經按過
    一個不可逆的確認鈕了。

    ⚠️ 這支**不分批**。分批是呼叫端的責任，因為只有它知道要顯示
    「第 2 / 5 批」的進度 —— 後端默默切的話，4,653 筆的等待時間裡畫面
    完全靜止，看起來像當掉。
    """
    if not body.ids:
        raise ApiError("bulk.no_ids", "ids is empty.")
    if len(body.ids) > BULK_ID_LIMIT:
        raise ApiError(
            "bulk.too_many_ids",
            f"At most {BULK_ID_LIMIT} ids at a time (got {len(body.ids)}) - "
            "SQLite binds at most 999 variables per statement, so the caller "
            "must split the work and show the progress.",
        )

    # ⚠️ 值域一律在這裡擋。DB 的 CHECK 是最後一道防線，不是錯誤訊息的來源 ——
    # 讓它擋下來的話使用者看到的是 500 + SQLite 的英文約束名，而他剛剛按的是
    # 一個改 4,653 筆的確認鈕。
    if body.stars is not None and body.stars != CLEAR:
        if not isinstance(body.stars, int) or isinstance(body.stars, bool):
            raise ApiError(
                "prefs.bad_stars",
                f"stars must be an integer 1-5, or '{CLEAR}' to clear it.")
        _validate_stars(body.stars)
    for name, allowed in (
        ("default_rating", Rating.values()),
        ("default_content_type", ContentType.values()),
    ):
        val = getattr(body, name)
        if val is not None and val != CLEAR and val not in allowed:
            raise ApiError(
                "prefs.bad_value",
                f"{name} must be one of {allowed}, or '{CLEAR}' to clear it.")

    # (欄位名, 改成什麼)
    changes: list[tuple[str, object]] = []
    for name in ("is_ignored", "is_tracked", "is_favorite"):
        val = getattr(body, name)
        if val is not None:
            changes.append((name, val))
    for name in ("stars", "default_rating", "default_content_type"):
        do, val = _bulk_value(getattr(body, name))
        if do:
            changes.append((name, val))

    if not changes:
        # 沒指定要改什麼卻送出，是呼叫端的 bug。靜默回 updated=0 會讓那個
        # bug 潛伏 —— 畫面顯示「改好 4,653 個」而一筆都沒動。
        raise ApiError("bulk.nothing_to_change", "No field was given to change.")

    rows = session.scalars(
        select(Account).where(Account.id.in_(body.ids))
    ).all()
    found = {a.id for a in rows}

    for acc in rows:
        for name, val in changes:
            setattr(acc, name, val)
            # 恢復追蹤要連 streak 歸零，理由同單筆那支。
            if name == "is_tracked" and val:
                acc.not_found_streak = 0
                acc.last_fetch_note = None
    session.commit()

    return {
        "updated": len(rows),
        # 選了但已經不存在的（期間被刪了）。逐筆回，不只回一個數字。
        "missing": sorted(set(body.ids) - found),
        "changed_fields": [name for name, _ in changes],
    }
