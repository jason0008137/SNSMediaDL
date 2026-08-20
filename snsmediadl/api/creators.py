"""Creators：跨平台與同平台小帳的歸屬。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.enums import AccountRole
from ..db.models import Account, Creator, Media, Post
from .app import get_session
from .errors import ApiError

router = APIRouter(prefix="/api", tags=["creators"])


class CreatorIn(BaseModel):
    display_name: str
    note: str | None = None


class CreatorPatch(BaseModel):
    display_name: str | None = None
    note: str | None = None


class LinkIn(BaseModel):
    creator_id: int
    role: str | None = None


def _creator_dict(c: Creator) -> dict:
    return {
        "id": c.id,
        "display_name": c.display_name,
        "note": c.note,
        "accounts": [
            {
                "id": a.id,
                "platform": a.platform,
                "screen_name": a.screen_name,
                "role": a.role,
            }
            for a in c.accounts
        ],
    }


@router.get("/creators")
def list_creators(session: Session = Depends(get_session)) -> list[dict]:
    return [_creator_dict(c) for c in session.scalars(select(Creator))]


@router.post("/creators", status_code=201)
def create_creator(body: CreatorIn, session: Session = Depends(get_session)) -> dict:
    creator = Creator(display_name=body.display_name, note=body.note)
    session.add(creator)
    session.commit()
    return _creator_dict(creator)


@router.get("/creators/{creator_id}")
def get_creator(creator_id: int, session: Session = Depends(get_session)) -> dict:
    creator = session.get(Creator, creator_id)
    if creator is None:
        raise ApiError("creator.not_found", "No such creator.", 404)
    return _creator_dict(creator)


@router.patch("/creators/{creator_id}")
def patch_creator(
    creator_id: int, body: CreatorPatch, session: Session = Depends(get_session)
) -> dict:
    creator = session.get(Creator, creator_id)
    if creator is None:
        raise ApiError("creator.not_found", "No such creator.", 404)
    if body.display_name is not None:
        creator.display_name = body.display_name
    if body.note is not None:
        creator.note = body.note
    session.commit()
    return _creator_dict(creator)


@router.post("/accounts/{account_id}/link")
def link_account(
    account_id: int, body: LinkIn, session: Session = Depends(get_session)
) -> dict:
    """把帳號掛到 creator。

    同一個 creator 可以掛任意數量的同平台帳號（本帳 + 小帳）——
    accounts 的唯一鍵是 (platform, platform_user_id)，刻意不含 creator_id。
    """
    account = session.get(Account, account_id)
    if account is None:
        raise ApiError("account.not_found", "No such account.", 404)
    if session.get(Creator, body.creator_id) is None:
        raise ApiError("creator.not_found", "No such creator.", 404)
    if body.role is not None and body.role not in AccountRole.values():
        raise ApiError(
            "link.bad_role", f"role must be one of {AccountRole.values()}.")

    account.creator_id = body.creator_id
    account.role = body.role
    session.commit()
    return {"account_id": account.id, "creator_id": account.creator_id, "role": account.role}


@router.delete("/accounts/{account_id}/link")
def unlink_account(account_id: int, session: Session = Depends(get_session)) -> dict:
    account = session.get(Account, account_id)
    if account is None:
        raise ApiError("account.not_found", "No such account.", 404)
    account.creator_id = None
    account.role = None
    session.commit()
    return {"account_id": account.id, "creator_id": None}


@router.get("/creators/{creator_id}/media")
def creator_media(creator_id: int, session: Session = Depends(get_session)) -> list[dict]:
    """該作者跨所有平台、所有帳號的媒體。"""
    if session.get(Creator, creator_id) is None:
        raise ApiError("creator.not_found", "No such creator.", 404)

    stmt = (
        select(Media)
        .join(Post, Media.post_id == Post.id)
        .join(Account, Post.account_id == Account.id)
        .where(Account.creator_id == creator_id)
        .order_by(Media.id)
    )
    return [
        {
            "id": m.id,
            "post_id": m.post_id,
            "kind": m.kind,
            "status": m.status,
            "source_url": m.source_url,
            "local_path": m.local_path,
        }
        for m in session.scalars(stmt)
    ]
