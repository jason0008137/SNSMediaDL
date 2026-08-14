"""查詢端點。"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Config
from ..db.enums import MediaStatus
from ..db.models import Account, Creator, Media, Post
from ..downloader import runner
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


@router.get("/accounts")
def list_accounts(
    platform: str | None = None,
    creator_id: int | None = None,
    q: str | None = Query(
        default=None, description="子字串搜尋，比對 screen_name 與 platform_user_id，不分大小寫"
    ),
    favorite: bool | None = Query(default=None, description="true 時只回我的最愛"),
    min_stars: int | None = Query(default=None, ge=1, le=5),
    sort: str = Query(
        default="id",
        description="favorite / stars / name / last_post / last_ingest / media / posts / created / id",
    ),
    order: str | None = Query(default=None, description="asc 或 desc，覆寫該排序的預設方向"),
    session: Session = Depends(get_session),
) -> list[dict]:
    """帳號清單。

    **回傳形狀刻意維持 list，不改成分頁 dict** —— `extension/bar.js` 直接把它
    當陣列跑 `.map`，換成 dict 不會拋錯，只會靜默變成空的下拉選單。

    `sort` 預設 `id`（= 這個功能加入之前的行為）。GUI 自己傳 `sort=favorite`。
    """
    # 聚合欄一次算完。做成 N+1（每個帳號各查一次）在 1,880 個帳號上就是
    # 1,881 個 query。
    post_agg = (
        select(
            Post.account_id.label("account_id"),
            func.count(Post.id).label("post_count"),
            func.max(Post.posted_at).label("last_post_at"),
            func.max(Post.ingested_at).label("last_ingest_at"),
        )
        .group_by(Post.account_id)
        .subquery()
    )
    media_agg = (
        select(
            Post.account_id.label("account_id"),
            func.count(Media.id).label("media_count"),
        )
        .join(Media, Media.post_id == Post.id)
        .group_by(Post.account_id)
        .subquery()
    )

    name_key = func.lower(func.coalesce(Account.screen_name, Account.platform_user_id))
    # (排序鍵, 預設是否 DESC)
    sorts = {
        "favorite": (Account.is_favorite, True),
        "stars": (Account.stars, True),
        "name": (name_key, False),
        "last_post": (post_agg.c.last_post_at, True),
        "last_ingest": (post_agg.c.last_ingest_at, True),
        "media": (func.coalesce(media_agg.c.media_count, 0), True),
        "posts": (func.coalesce(post_agg.c.post_count, 0), True),
        "created": (Account.created_at, True),
        "id": (Account.id, False),
    }
    if sort not in sorts:
        # 不默默退回預設 —— 那會讓「參數打錯」看起來像「排序功能壞了」。
        raise HTTPException(422, f"sort 必須是 {sorted(sorts)} 之一")
    if order is not None and order not in ("asc", "desc"):
        raise HTTPException(422, "order 必須是 asc 或 desc")

    stmt = (
        select(
            Account,
            post_agg.c.post_count,
            post_agg.c.last_post_at,
            post_agg.c.last_ingest_at,
            media_agg.c.media_count,
        )
        .outerjoin(post_agg, post_agg.c.account_id == Account.id)
        .outerjoin(media_agg, media_agg.c.account_id == Account.id)
    )

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

    key, default_desc = sorts[sort]
    desc = default_desc if order is None else order == "desc"
    # ⚠️ SQLite 把 NULL 當最小值，DESC 時會排到**最前面** —— 未評分的帳號
    # 會壓在五星前面。所有可為 NULL 的鍵一律 nullslast。
    primary = (key.desc() if desc else key.asc()).nullslast()
    # 「我的最愛」只有兩個值，沒有次要鍵的話組內順序等於隨機。
    tiebreak: list = []
    if sort == "favorite":
        tiebreak = [Account.stars.desc().nullslast(), name_key.asc()]
    elif sort == "stars":
        tiebreak = [name_key.asc()]
    # id 收尾：所有鍵都相同時，順序至少要在多次請求間穩定，否則分頁會跳。
    stmt = stmt.order_by(primary, *tiebreak, Account.id.asc())

    out = []
    for account, post_count, last_post_at, last_ingest_at, media_count in session.execute(stmt):
        d = _account_dict(account)
        d.update(
            post_count=post_count or 0,
            media_count=media_count or 0,
            last_post_at=last_post_at.isoformat() if last_post_at else None,
            last_ingest_at=last_ingest_at.isoformat() if last_ingest_at else None,
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
    session: Session = Depends(get_session),
) -> dict:
    if sort not in ("newest", "oldest", "stars"):
        raise HTTPException(422, "sort 必須是 newest / oldest / stars 之一")
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

    if sort == "stars":
        # nullslast：未評分不能壓在五星前面。id 收尾讓同星等內順序穩定。
        stmt = stmt.order_by(Media.stars.desc().nullslast(), Media.id.desc())
    elif sort == "oldest":
        stmt = stmt.order_by(Media.id.asc())
    else:
        stmt = stmt.order_by(Media.id.desc())
    return _paged(session, stmt, limit, offset, _media_dict)


@router.get("/queue/status")
def queue_status(session: Session = Depends(get_session)) -> dict:
    rows = session.execute(
        select(Media.status, func.count()).group_by(Media.status)
    ).all()
    counts = {s.value: 0 for s in MediaStatus}
    counts.update({status: n for status, n in rows})
    counts["total"] = sum(n for status, n in rows)
    # 呼叫端要能分辨「還沒開始」與「正在跑」——只看 pending 數字兩者長得一樣，
    # 而 extension 的進度顯示必須知道該不該繼續等。
    counts["running"] = runner.is_running()
    counts["last_run"] = runner.last_run()
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
    return {
        "media": _media_dict(m),
        "post": _post_dict(p),
        "account": _account_dict(a),
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
