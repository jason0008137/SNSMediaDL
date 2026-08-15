"""查詢端點。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..db.enums import ContentType, FetchStatus, MediaStatus, Rating
from ..db.models import Account, Creator, Media, Post
from ..downloader import runner
from ..services.ingest import last_ingest
from . import logbuf
from .app import get_config, get_maker, get_session, get_transport

log = logging.getLogger("snsmediadl")

router = APIRouter(prefix="/api", tags=["query"])

# asyncio 對執行中的 task 只持有弱參照 —— 不自己留著，長時間的下載可能被 GC 掉
_running_tasks: set[asyncio.Task] = set()


def _paged(session: Session, stmt, limit: int, offset: int, mapper) -> dict:
    """分頁一律回總數。

    只回陣列的話，前端無從得知還有沒有下一頁，只能瞎猜。
    """
    total = session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    rows = session.scalars(stmt.limit(limit).offset(offset))
    return {
        "items": [mapper(r) for r in rows],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


def _account_dict(a: Account) -> dict:
    return {
        "id": a.id,
        "platform": a.platform,
        "instance_host": a.instance_host,
        "platform_user_id": a.platform_user_id,
        "screen_name": a.screen_name,
        "is_tracked": a.is_tracked,
        "creator_id": a.creator_id,
        "role": a.role,
        "default_rating": a.default_rating,
        "default_content_type": a.default_content_type,
        # 個人偏好。與 default_rating 無關 —— 那是會繼承給貼文的分級，
        # 這兩個只是「我多喜歡這個帳號」。
        "is_favorite": a.is_favorite,
        "stars": a.stars,
        # ⚠️ 這是「最後一次**嘗試**擷取」，不是最後一次成功。
        # 與 last_ingest_at（最後一次抓到新東西）是不同的問題。
        "last_fetched_at": a.last_fetched_at.isoformat() if a.last_fetched_at else None,
        "last_fetch_status": a.last_fetch_status,
        "last_fetch_note": a.last_fetch_note,
        "last_fetch_new_posts": a.last_fetch_new_posts,
    }


def _post_dict(p: Post) -> dict:
    return {
        "id": p.id,
        "platform": p.platform,
        "platform_post_id": p.platform_post_id,
        "account_id": p.account_id,
        "posted_at": p.posted_at.isoformat() if p.posted_at else None,
        "is_retweet": p.is_retweet,
        "rating": p.rating,
        "content_type": p.content_type,
        "rating_source": p.rating_source,
        "media_count": len(p.media),
    }


def _media_dict(m: Media) -> dict:
    return {
        "id": m.id,
        "post_id": m.post_id,
        "ordinal": m.ordinal,
        "kind": m.kind,
        "source_url": m.source_url,
        "local_path": m.local_path,
        "file_hash": m.file_hash,
        "bytes": m.bytes,
        "status": m.status,
        "error": m.error,
        "attempt_count": m.attempt_count,
        "stars": m.stars,
    }


def _like_pattern(q: str) -> str:
    """把使用者輸入的子字串轉成安全的 LIKE pattern。

    `_` 與 `%` 在 LIKE 裡是萬用字元，必須跳脫。這裡不是理論上的潔癖 ——
    帳號名稱含底線是常態（`heikala_art`），不跳脫的話搜 `heikala_art`
    會連 `heikalaXart` 一起撈出來，而使用者只會覺得「搜尋怪怪的」。
    """
    esc = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


# 「這個欄位還沒設過」的哨符。空字串不行 —— `?x=` 與不帶參數在
# query string 裡分不出來，而「還沒設過的」正是最主要的用例。
UNSET = "__unset__"


def _filter_unset_or_value(stmt, column, value: str | None,
                           allowed: list[str], name: str):
    """套用「等於某值」或「是 NULL」的篩選。值域不對就 422，不默默忽略。"""
    # ⚠️ 空字串 = 不篩選，與「沒帶這個參數」等價。
    # FastAPI 把 `?default_rating=` 解析成 `""` 而不是 `None`，而前端「清除篩選」
    # 常常就是送一個空值。不吃這一種的話，清除篩選會變成 422。
    # 這也正是需要 `__unset__` 哨符的理由：空字串這個位置已經被佔走了。
    if value is None or value == "":
        return stmt
    if value == UNSET:
        return stmt.where(column.is_(None))
    if value not in allowed:
        # 不默默當成「不篩選」—— 那會讓打錯字看起來像「篩選功能壞了」
        raise HTTPException(
            422, f"{name} 必須是 {allowed} 之一，或 '{UNSET}'（未設定）")
    return stmt.where(column == value)


@router.get("/accounts")
def list_accounts(
    platform: str | None = None,
    creator_id: int | None = None,
    q: str | None = Query(
        default=None, description="子字串搜尋，比對 screen_name 與 platform_user_id，不分大小寫"
    ),
    favorite: bool | None = Query(default=None, description="true 時只回我的最愛"),
    min_stars: int | None = Query(default=None, ge=1, le=5),
    fetch_status: str | None = Query(
        default=None,
        description="只回最後一次擷取是這些結果的帳號，逗號分隔可給多個。"
                    "例：`not_found,rate_limited,auth_required,failed` "
                    "= 一次列出所有「抓取有問題」的帳號",
    ),
    default_rating: str | None = Query(
        default=None,
        description="依帳號預設分級篩選。`__unset__` = 還沒設過的（主要用例）",
    ),
    default_content_type: str | None = Query(
        default=None,
        description="依帳號預設類型篩選。`__unset__` = 還沒設過的",
    ),
    sort: str = Query(
        default="id",
        description="favorite / stars / name / last_post / last_ingest / last_fetch"
                    " / media / posts / created / id",
    ),
    order: str | None = Query(default=None, description="asc 或 desc，覆寫該排序的預設方向"),
    limit: int | None = Query(
        default=None, ge=1, le=2000,
        description="不給就是全部。**GUI 一定要給** —— 4,653 個帳號一次渲染會把瀏覽器凍住",
    ),
    offset: int = 0,
    with_stats: bool = Query(
        default=True,
        description="關掉就不算貼文數／媒體數／最後發文等聚合欄。"
                    "只要名字的用途（例如下拉選單）關掉會快很多",
    ),
    response: Response = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
) -> list[dict]:
    """帳號清單。

    **回傳形狀刻意維持 list，不改成分頁 dict** —— `extension/bar.js` 直接把它
    當陣列跑 `.map`，換成 dict 不會拋錯，只會靜默變成空的下拉選單。

    `sort` 預設 `id`（= 這個功能加入之前的行為）。GUI 自己傳 `sort=favorite`。
    """
    # 聚合欄現在**是 accounts 上的欄位**，不再即席算。
    #
    # 演進史（三代，每一代都有實測數字支撐）：
    #   1. GROUP BY 子查詢再 outerjoin ......... 4,900 ms（掃完整張 posts + media）
    #   2. 相關純量子查詢 + 分頁 ................ 436 ms，但 sort=media 仍 1,906 ms
    #   3. 去正規化成欄位（現在）................ 讀欄位，毫秒級
    #
    # 第 2 代的硬底線在 `sort=media`：排序鍵需要**全部** 4,653 個帳號的
    # media_count，分頁救不了。只有把值存下來能解。
    #
    # ⚠️ 代價是快取值會失準。維護與檢查一律走 `services/counters.py`，
    # 別在這裡或任何地方手動加減。`snsmediadl recount-accounts --check`
    # 會比對它們與真值。
    post_count = Account.post_count
    last_post_at = Account.last_post_at
    last_ingest_at = Account.last_ingest_at
    media_count = Account.media_count

    name_key = func.lower(func.coalesce(Account.screen_name, Account.platform_user_id))
    # (排序鍵, 預設是否 DESC)
    sorts = {
        "favorite": (Account.is_favorite, True),
        "stars": (Account.stars, True),
        "name": (name_key, False),
        "last_post": (last_post_at, True),
        "last_ingest": (last_ingest_at, True),
        "last_fetch": (Account.last_fetched_at, False),
        "media": (media_count, True),
        "posts": (post_count, True),
        "created": (Account.created_at, True),
        "id": (Account.id, False),
    }
    if sort not in sorts:
        # 不默默退回預設 —— 那會讓「參數打錯」看起來像「排序功能壞了」。
        raise HTTPException(422, f"sort 必須是 {sorted(sorts)} 之一")
    if order is not None and order not in ("asc", "desc"):
        raise HTTPException(422, "order 必須是 asc 或 desc")

    # 去正規化之後聚合欄不再有查詢成本（它們就在 accounts 上），所以
    # `with_stats` 現在只影響**回應大小**，不影響速度。參數保留 ——
    # extension 的下拉只要名字，少四個欄位仍然省一點傳輸。
    #
    # 排序鍵用到聚合欄時一律當成要 stats，否則排出來的順序與顯示的數字對不上。
    needs_stats = with_stats or sort in ("last_post", "last_ingest", "media", "posts")
    stmt = select(Account)

    if platform:
        stmt = stmt.where(Account.platform == platform)
    if creator_id is not None:
        stmt = stmt.where(Account.creator_id == creator_id)
    if q:
        pattern = _like_pattern(q)
        stmt = stmt.where(
            func.lower(func.coalesce(Account.screen_name, "")).like(pattern, escape="\\")
            | func.lower(Account.platform_user_id).like(pattern, escape="\\")
        )
    if favorite:
        stmt = stmt.where(Account.is_favorite.is_(True))
    if min_stars is not None:
        # NULL（未評分）不算 0 分，要被濾掉 —— SQL 的 `stars >= 1` 本來就會
        # 排除 NULL，這裡只是講明白這是刻意的。
        stmt = stmt.where(Account.stars >= min_stars)
    if fetch_status:
        # 支援逗號分隔的多值。「只看抓取有問題的」是把四種失敗狀態放在一起 ——
        # 那必須在**後端**篩，前端只濾當頁的話，使用者會在一頁全是「從沒檢查過」
        # 的清單上看到 0 筆，然後以為沒有問題。實際上有。
        wanted = [s.strip() for s in fetch_status.split(",") if s.strip()]
        bad = [s for s in wanted if s not in FetchStatus.values()]
        if bad:
            raise HTTPException(
                422, f"fetch_status 含未知的值 {bad}；可用：{FetchStatus.values()}")
        stmt = stmt.where(Account.last_fetch_status.in_(wanted))

    # 帳號預設值篩選。
    #
    # ⚠️ **NULL 用 `__unset__` 這個哨符，不用空字串。** 空字串與「不篩選」
    # 在 query string 裡長得一模一樣（`?default_rating=` 就是空字串），
    # 分不出來的話「找出還沒設過的帳號」這個功能根本表達不出來。
    #
    # 而那正是主要用例：正式庫 4,653 個帳號**全部**沒設過預設值，使用者要的
    # 就是「哪些我還沒標」。
    stmt = _filter_unset_or_value(
        stmt, Account.default_rating, default_rating, Rating.values(), "default_rating")
    stmt = _filter_unset_or_value(
        stmt, Account.default_content_type, default_content_type,
        ContentType.values(), "default_content_type")

    key, default_desc = sorts[sort]
    desc = default_desc if order is None else order == "desc"
    # ⚠️ SQLite 把 NULL 當最小值，DESC 時會排到**最前面** —— 未評分的帳號
    # 會壓在五星前面。所有可為 NULL 的鍵一律 nullslast。
    if sort == "last_fetch":
        # ⚠️ **刻意與其他排序鍵相反**：這裡 NULL 排最前面，不是 nullslast。
        # 「從沒查過」就是「最該查」——「最久沒檢查的排前面」正是這個排序
        # 存在的理由，把從沒查過的沉到底等於把答案藏起來。
        primary = key.asc().nullsfirst() if not desc else key.desc().nullslast()
    else:
        primary = (key.desc() if desc else key.asc()).nullslast()
    # 「我的最愛」只有兩個值，沒有次要鍵的話組內順序等於隨機。
    tiebreak: list = []
    if sort == "favorite":
        tiebreak = [Account.stars.desc().nullslast(), name_key.asc()]
    elif sort == "stars":
        tiebreak = [name_key.asc()]
    # id 收尾：所有鍵都相同時，順序至少要在多次請求間穩定，否則分頁會跳。
    stmt = stmt.order_by(primary, *tiebreak, Account.id.asc())

    # 總數走 header 而不是包成 {items, total}：`extension/bar.js` 直接把
    # 回應當陣列跑 .map，換成 dict 不會拋錯，只會靜默變成空的下拉選單。
    if response is not None:
        total = session.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        )
        response.headers["X-Total-Count"] = str(total or 0)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)

    out = []
    for account in session.scalars(stmt):
        d = _account_dict(account)
        if needs_stats:
            d.update(
                post_count=account.post_count or 0,
                media_count=account.media_count or 0,
                last_post_at=(account.last_post_at.isoformat()
                              if account.last_post_at else None),
                last_ingest_at=(account.last_ingest_at.isoformat()
                                if account.last_ingest_at else None),
            )
        out.append(d)
    return out


@router.get("/posts")
def list_posts(
    platform: str | None = None,
    account_id: int | None = None,
    creator_id: int | None = None,
    rating: str | None = None,
    exclude_rating: str | None = Query(
        default=None,
        description="排除某個分級。工作環境瀏覽時排除 r18 是高頻需求，"
                    "做成一等公民參數，不要逼使用者自己組條件。",
    ),
    content_type: str | None = None,
    limit: int = Query(default=200, le=2000),
    offset: int = 0,
    session: Session = Depends(get_session),
) -> dict:
    stmt = select(Post)
    if platform:
        stmt = stmt.where(Post.platform == platform)
    if account_id is not None:
        stmt = stmt.where(Post.account_id == account_id)
    if creator_id is not None:
        stmt = stmt.join(Account, Post.account_id == Account.id).where(
            Account.creator_id == creator_id
        )
    if rating:
        stmt = stmt.where(Post.rating == rating)
    if exclude_rating:
        # NULL 是「未知」，不等於被排除的那一級 —— 必須明確保留，
        # 否則 `!= 'r18'` 在 SQL 裡會把 NULL 一起濾掉。
        stmt = stmt.where(
            (Post.rating.is_(None)) | (Post.rating != exclude_rating)
        )
    if content_type:
        stmt = stmt.where(Post.content_type == content_type)

    stmt = stmt.order_by(Post.posted_at.desc().nullslast())
    return _paged(session, stmt, limit, offset, _post_dict)


MEDIA_SORTS = ("newest", "oldest", "stars")


def _media_stmt(
    *,
    status: str | None,
    kind: str | None,
    rating: str | None,
    exclude_rating: str | None,
    content_type: str | None,
    account_id: int | None,
    creator_id: int | None,
    platform: str | None,
    min_stars: int | None,
):
    """媒體篩選條件。**清單與總數共用同一份**。

    兩邊各寫一次條件的話，改了一邊忘了另一邊，症狀是「總數與實際看到的筆數對不上」
    —— 而那看起來像分頁壞了，不像篩選寫錯，會查很久。
    """
    stmt = select(Media)
    needs_post = any(
        [rating, exclude_rating, content_type, account_id, creator_id, platform]
    )
    if needs_post:
        stmt = stmt.join(Post, Media.post_id == Post.id)
    if creator_id is not None:
        stmt = stmt.join(Account, Post.account_id == Account.id).where(
            Account.creator_id == creator_id
        )
    if account_id is not None:
        stmt = stmt.where(Post.account_id == account_id)
    if platform:
        stmt = stmt.where(Post.platform == platform)
    if status:
        stmt = stmt.where(Media.status == status)
    if kind:
        stmt = stmt.where(Media.kind == kind)
    if rating:
        stmt = stmt.where(Post.rating == rating)
    if exclude_rating:
        stmt = stmt.where((Post.rating.is_(None)) | (Post.rating != exclude_rating))
    if content_type:
        stmt = stmt.where(Post.content_type == content_type)
    if min_stars is not None:
        stmt = stmt.where(Media.stars >= min_stars)
    return stmt


@router.get("/media")
def list_media(
    status: str | None = None,
    kind: str | None = None,
    rating: str | None = None,
    exclude_rating: str | None = None,
    content_type: str | None = None,
    account_id: int | None = None,
    creator_id: int | None = None,
    platform: str | None = None,
    min_stars: int | None = Query(
        default=None, ge=1, le=5,
        description="只回 ≥ N 星。⚠️ 這是五星評分，與 rating（sfw/r18 分級）無關",
    ),
    sort: str = Query(default="newest", description="newest / oldest / stars"),
    limit: int = Query(default=60, le=500),
    offset: int = 0,
    before_id: int | None = Query(
        default=None,
        description="keyset 分頁游標：只回 id 比它小的（配 sort=newest）。"
                    "與 offset 互斥，有給就忽略 offset",
    ),
    after_id: int | None = Query(
        default=None, description="keyset 分頁游標：只回 id 比它大的（配 sort=oldest）",
    ),
    session: Session = Depends(get_session),
) -> dict:
    """媒體清單。

    ### ⚠️ 這支**不回總數**

    總數在 `GET /api/media/count`，是獨立的一次請求。

    理由是實測：在 224 萬筆的正式庫上，這一頁的資料本身要 1 ms，而
    `COUNT(*)` 要 1,311 ms —— 也就是 99.9% 的等待時間花在一個「還有幾筆」的
    數字上。而那個數字每次翻頁、每次改篩選、甚至每次評分存檔都要重算一次。

    COUNT 慢不是索引沒建好，是**沒有索引救得了**：安全模式的
    `exclude_rating='r18'` 要保留 94.9% 的資料（sfw 154 萬 / r18 8.3 萬），
    選擇性太差，planner 不會也不該用索引。

    所以拆開：清單先到、畫面先出來，總數晚一點自己補上。

    ⚠️ 呼叫端**不得**在總數還沒到時顯示 0 或空白 —— 那是拿假資料填空窗，
    使用者會以為真的沒有東西。算不出來就顯示「計算中」或錯誤。

    ### 分頁：keyset 與 offset

    `sort=newest` / `oldest` 走 **keyset**（`before_id` / `after_id`）：
    翻到第幾頁都是同樣的成本。

    `sort=stars` 仍走 offset。它的排序鍵是 `(stars, id)` 複合又含 NULL，
    keyset 條件寫起來容易錯，而正式庫裡 `stars` 目前 100% 是 NULL ——
    為一條沒人走的路加一段難驗證的邏輯不划算。深頁時會慢，這一點寫在這裡，
    不是靜默的。
    """
    if sort not in MEDIA_SORTS:
        raise HTTPException(422, f"sort 必須是 {' / '.join(MEDIA_SORTS)} 之一")
    if before_id is not None and after_id is not None:
        raise HTTPException(422, "before_id 與 after_id 不能同時給")
    if sort == "stars" and (before_id is not None or after_id is not None):
        # 默默改用 offset 會讓呼叫端以為自己在做 keyset，翻頁時靜默跳筆。
        raise HTTPException(422, "sort=stars 不支援 keyset 分頁，請用 offset")

    stmt = _media_stmt(
        status=status, kind=kind, rating=rating, exclude_rating=exclude_rating,
        content_type=content_type, account_id=account_id, creator_id=creator_id,
        platform=platform, min_stars=min_stars,
    )

    if before_id is not None:
        stmt = stmt.where(Media.id < before_id)
    if after_id is not None:
        stmt = stmt.where(Media.id > after_id)

    if sort == "stars":
        # nullslast：未評分不能壓在五星前面。id 收尾讓同星等內順序穩定。
        stmt = stmt.order_by(Media.stars.desc().nullslast(), Media.id.desc())
    elif sort == "oldest":
        stmt = stmt.order_by(Media.id.asc())
    else:
        stmt = stmt.order_by(Media.id.desc())

    using_keyset = before_id is not None or after_id is not None
    # 多撈一筆來判斷「還有沒有下一頁」。這是 has_more 唯一不需要 COUNT 的做法。
    rows = list(session.scalars(
        stmt.limit(limit + 1).offset(0 if using_keyset else offset)
    ))
    has_more = len(rows) > limit
    rows = rows[:limit]

    cursor = rows[-1].id if rows else None
    return {
        "items": [_media_dict(m) for m in rows],
        "limit": limit,
        "offset": 0 if using_keyset else offset,
        "has_more": has_more,
        # 下一頁的游標。sort=oldest 時呼叫端要拿它當 after_id。
        "next_before_id": cursor if sort == "newest" else None,
        "next_after_id": cursor if sort == "oldest" else None,
    }


# ⚠️ 必須宣告在 `/media/{media_id}` **之前**。FastAPI 依宣告順序比對路由，
# 反過來的話 `/api/media/count` 會被 `{media_id}` 接走，然後因為 "count"
# 不是 int 而回 422 —— 不會 fallthrough 到這一支。
@router.get("/media/count")
def count_media(
    status: str | None = None,
    kind: str | None = None,
    rating: str | None = None,
    exclude_rating: str | None = None,
    content_type: str | None = None,
    account_id: int | None = None,
    creator_id: int | None = None,
    platform: str | None = None,
    min_stars: int | None = Query(default=None, ge=1, le=5),
    session: Session = Depends(get_session),
) -> dict:
    """符合條件的媒體總數。參數與 `GET /api/media` 的篩選參數完全相同。

    從 `/api/media` 拆出來的理由見那一支的說明。**這支很慢是預期中的**
    （正式庫上約 1.3 秒），所以呼叫端要非同步發、不要擋著畫面。
    """
    def count(stmt) -> int:
        return session.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ) or 0

    total = count(_media_stmt(
        status=status, kind=kind, rating=rating, exclude_rating=exclude_rating,
        content_type=content_type, account_id=account_id, creator_id=creator_id,
        platform=platform, min_stars=min_stars,
    ))

    # ── 被安全模式擋掉幾筆 ──
    #
    # ⚠️ 這是**第二次 COUNT**，成本翻倍。所以只在 `total == 0` 時才算 ——
    # 那正是使用者最困惑的時刻：帳號頁明明寫著「684 個媒體」，點進來卻是空的。
    # 有結果的時候這個數字幫助有限，不值得再掃一次 224 萬列。
    #
    # 沒算的時候回 `None`，**不回 0** —— 0 的意思是「確定沒有被擋掉的」，
    # 那是另一件事。呼叫端要能分辨「沒被擋」與「沒去算」。
    hidden: int | None = None
    if exclude_rating and total == 0:
        hidden = count(_media_stmt(
            status=status, kind=kind, rating=rating, exclude_rating=None,
            content_type=content_type, account_id=account_id, creator_id=creator_id,
            platform=platform, min_stars=min_stars,
        ))

    return {"total": total, "hidden_by_safe_mode": hidden}


# 佇列真正在動的三個狀態。`done` 不在裡面 —— 見 `queue_status`。
ACTIVE_STATUSES = (
    MediaStatus.PENDING.value,
    MediaStatus.DOWNLOADING.value,
    MediaStatus.FAILED.value,
)


@router.get("/queue/status")
def queue_status(session: Session = Depends(get_session)) -> dict:
    """佇列狀態。**每 5 秒被輪詢一次，所以成本是第一考量。**

    ### `done` 與 `total` 回 `None`，那不是漏掉

    舊寫法是 `GROUP BY media.status`。在正式庫上量到 **412 ms**，而且
    `EXPLAIN` 顯示 `SCAN media USING COVERING INDEX ix_media_status` ——
    掃完 224 萬個 index entry，只為了得出 `done = 2,247,825`。
    那個數字回答不了任何決策：它是整個媒體庫的大小，不是「佇列還剩多少」。

    改成對三個 active 狀態各做一次 `status = ?` 的 index seek，量到 **0 ms**
    （實際各只碰 21 / 4 / 0 列）。

    ⚠️ **不要把這三次 seek 併成 `status != 'done'` 或 `status IN (...)`。**
    實測（SQLite 3.40.1）：只有寫成 `= ?` 才會走 `SEARCH ... USING INDEX`；
    `!=` 與 `IN` 都會退回 `SCAN media` 全表掃描。這條是量出來的，不是猜的。

    ⚠️ `done` 回 `None` 而不是 0 —— 0 是謊話（正式庫有 224 萬筆）。
    呼叫端拿到 `None` 要顯示「未計算」或乾脆不顯示，**不可當成 0**。
    要精確總數請打 `GET /api/stats`，那是使用者明確按的動作。
    """
    counts: dict = {
        status: session.scalar(
            select(func.count()).select_from(Media).where(Media.status == status)
        ) or 0
        for status in ACTIVE_STATUSES
    }
    counts["active"] = sum(counts.values())
    # 明確標成「沒算」。省略欄位的話，呼叫端的 `q.done || 0` 會靜默變成 0。
    counts["done"] = None
    counts["total"] = None
    counts["done_exact"] = False
    # 呼叫端要能分辨「還沒開始」與「正在跑」——只看 pending 數字兩者長得一樣，
    # 而 extension 的進度顯示必須知道該不該繼續等。
    counts["running"] = runner.is_running()
    counts["last_run"] = runner.last_run()
    # 第三條背景流程（extension 採集）。GUI 的背景活動區要三條分列：
    # 下載 worker、抓取佇列、extension 採集**互不相干**，任何一條在跑不代表
    # 另外兩條也在跑 —— 共用一個數字會被讀成「系統的狀態」。
    counts["last_ingest"] = last_ingest()
    return counts


@router.post("/queue/run")
async def run_queue(
    cfg: Config = Depends(get_config),
    session: Session = Depends(get_session),
) -> dict:
    """把佇列跑一輪。**明確觸發，不受 `auto_download` 影響。**

    存在的理由：`POST /api/ingest` 只入庫排隊，而背景迴圈預設是關的 ——
    在這個端點出現之前，extension 上那顆「送出並下載」根本不會下載任何東西，
    而且還回報成功。那正是本專案最忌諱的靜默失敗。

    不阻塞 request：下載可能跑好幾分鐘。這裡只負責啟動，
    進度由 `GET /api/queue/status` 查。

    必須是 `async def`：同步端點會被 FastAPI 丟到 threadpool 執行，
    那裡沒有 running loop，`asyncio.create_task` 會直接丟 RuntimeError。
    """
    pending = session.scalar(
        select(func.count()).select_from(Media).where(
            Media.status == MediaStatus.PENDING.value
        )
    ) or 0

    if runner.is_running():
        return {"started": False, "already_running": True, "pending": pending}

    # create_task 而不是 await：讓 response 立刻回去。
    # 任務的參照要留著，否則可能被 GC 掉（asyncio 只持有弱參照）。
    task = asyncio.create_task(
        runner.run_once(cfg, get_maker(), transport=get_transport())
    )
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)

    return {"started": True, "already_running": False, "pending": pending}


@router.get("/known")
def known_posts(
    platform: str,
    post_ids: str = Query(description="逗號分隔的平台貼文 ID"),
    session: Session = Depends(get_session),
) -> dict:
    """給 extension 問「這些我抓過了嗎」。"""
    wanted = [p.strip() for p in post_ids.split(",") if p.strip()]
    if not wanted:
        return {"known": []}
    found = session.scalars(
        select(Post.platform_post_id).where(
            Post.platform == platform,
            Post.platform_post_id.in_(wanted),
        )
    ).all()
    return {"known": list(found)}


@router.get("/media/{media_id}")
def get_media_detail(
    media_id: int, session: Session = Depends(get_session)
) -> dict:
    """單筆媒體 + 所屬貼文 + 帳號。

    詳情面板專用。先前是抓整個清單再從裡面找，資料量一大就必然找不到 ——
    要一筆資料就抓一筆。
    """
    row = session.execute(
        select(Media, Post, Account)
        .join(Post, Media.post_id == Post.id)
        .join(Account, Post.account_id == Account.id)
        .where(Media.id == media_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="media not found")

    m, p, a = row
    # 同一則貼文的其他張。分級掛在**貼文**上，所以詳情面板要讓使用者看得出
    # 「改這裡會影響幾張」——正式庫單則貼文最多 **194 個媒體**，那不是理論值。
    # 只回 id / 序號 / 型別：面板上是一排小按鈕，不需要整份 media_dict。
    siblings = session.execute(
        select(Media.id, Media.ordinal, Media.kind)
        .where(Media.post_id == p.id)
        .order_by(Media.ordinal, Media.id)
    ).all()
    return {
        "media": _media_dict(m),
        "post": _post_dict(p),
        "account": _account_dict(a),
        "siblings": [
            {"id": sid, "ordinal": ordinal, "kind": kind}
            for sid, ordinal, kind in siblings
        ],
    }


@router.get("/stats")
def stats(session: Session = Depends(get_session)) -> dict:
    """首頁摘要。"""
    by_status = dict(
        session.execute(select(Media.status, func.count()).group_by(Media.status)).all()
    )
    by_rating = {
        (r or "unrated"): n
        for r, n in session.execute(
            select(Post.rating, func.count()).group_by(Post.rating)
        ).all()
    }
    by_kind = dict(
        session.execute(select(Media.kind, func.count()).group_by(Media.kind)).all()
    )
    return {
        "media_total": session.scalar(select(func.count()).select_from(Media)) or 0,
        "post_total": session.scalar(select(func.count()).select_from(Post)) or 0,
        "account_total": session.scalar(select(func.count()).select_from(Account)) or 0,
        "creator_total": session.scalar(select(func.count()).select_from(Creator)) or 0,
        "by_status": by_status,
        "by_rating": by_rating,
        "by_kind": by_kind,
        "bytes_total": session.scalar(select(func.coalesce(func.sum(Media.bytes), 0))) or 0,
    }


@router.get("/errors")
def list_errors(
    limit: int = Query(default=100, le=1000),
    session: Session = Depends(get_session),
) -> dict:
    """失敗的媒體。沒有這個端點的話，失敗只會表現成「數字對不上」。"""
    stmt = (
        select(Media, Post, Account)
        .join(Post, Media.post_id == Post.id)
        .join(Account, Post.account_id == Account.id)
        .where(Media.status == MediaStatus.FAILED.value)
        .order_by(Media.id.desc())
        .limit(limit)
    )
    items = [
        {
            "media_id": m.id,
            "kind": m.kind,
            "source_url": m.source_url,
            "error": m.error,
            "attempt_count": m.attempt_count,
            "post_id": p.platform_post_id,
            "platform": p.platform,
            "screen_name": a.screen_name,
        }
        for m, p, a in session.execute(stmt).all()
    ]
    return {"items": items, "total": len(items)}


@router.post("/media/retry-failed")
def retry_all_failed(session: Session = Depends(get_session)) -> dict:
    """把所有失敗的打回佇列。"""
    rows = session.scalars(
        select(Media).where(Media.status == MediaStatus.FAILED.value)
    ).all()
    for m in rows:
        m.status = MediaStatus.PENDING.value
        m.error = None
    session.commit()
    return {"requeued": len(rows)}


@router.get("/settings")
def get_settings(cfg: Config = Depends(get_config)) -> dict:
    return {
        "auto_download": cfg.auto_download,
        "concurrency": cfg.concurrency,
        "download_delay_seconds": cfg.download_delay_seconds,
        "poll_interval_seconds": cfg.poll_interval_seconds,
        "output_root": str(cfg.output_root),
        # 唯讀。這是「預設值應該是什麼」，改法是 config.toml + 重啟，
        # 不是執行期 PATCH（見 patch_settings 的說明）。
        "extra_media_roots": [str(p) for p in cfg.extra_media_roots],
        # 設定面板要把「這些改不動、要編 config.toml 並重啟」講出來 ——
        # 看得見的約束才不會被誤當成壞掉的控制項。
        "thumb_root": str(cfg.thumb_dir),
        "fetch_max_pages": cfg.fetch_max_pages,
    }


class SettingsPatch(BaseModel):
    auto_download: bool | None = None


@router.patch("/settings")
def patch_settings(body: SettingsPatch, cfg: Config = Depends(get_config)) -> dict:
    """執行期切換。背景迴圈每輪重讀，所以立即生效、不用重啟。

    刻意不寫回 config.toml —— 這是「現在要不要跑」的暫時決定，
    不是「預設應該是什麼」。
    """
    if body.auto_download is not None:
        cfg.auto_download = body.auto_download
        log.info("背景下載已%s", "開啟" if body.auto_download else "關閉")
    return {"auto_download": cfg.auto_download}


@router.get("/logs")
def get_logs(limit: int = Query(default=200, le=500), level: str | None = None) -> dict:
    return {"items": logbuf.records(limit=limit, level=level)}


@router.post("/media/{media_id}/retry")
def retry_media(media_id: int, session: Session = Depends(get_session)) -> dict:
    """把失敗的媒體打回佇列。強制重抓是明確動作，不是預設行為。"""
    media = session.get(Media, media_id)
    if media is None:
        raise HTTPException(status_code=404, detail="media not found")
    media.status = MediaStatus.PENDING.value
    media.error = None
    session.commit()
    return {"id": media.id, "status": media.status}
