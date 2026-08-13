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
        "platform_user_id": a.platform_user_id,
        "screen_name": a.screen_name,
        "is_tracked": a.is_tracked,
        "creator_id": a.creator_id,
        "role": a.role,
        "default_rating": a.default_rating,
        "default_content_type": a.default_content_type,
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
    }


@router.get("/accounts")
def list_accounts(
    platform: str | None = None,
    creator_id: int | None = None,
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(Account)
    if platform:
        stmt = stmt.where(Account.platform == platform)
    if creator_id is not None:
        stmt = stmt.where(Account.creator_id == creator_id)
    return [_account_dict(a) for a in session.scalars(stmt)]


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
    limit: int = Query(default=60, le=500),
    offset: int = 0,
    session: Session = Depends(get_session),
) -> dict:
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
