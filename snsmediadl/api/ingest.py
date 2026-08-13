"""採集入口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..services.ingest import ingest
from .app import get_session

router = APIRouter(prefix="/api", tags=["ingest"])


class IngestRequest(BaseModel):
    platform: str = "x"
    screen_name: str | None = Field(default=None, alias="screenName")
    posts: list[dict[str, Any]]

    model_config = {"populate_by_name": True}


@router.post("/ingest")
def post_ingest(body: IngestRequest, session: Session = Depends(get_session)) -> dict:
    """接收採集結果。只入庫排隊，不在 request 內下載。"""
    result = ingest(session, body.platform, body.posts, screen_name=body.screen_name)
    return result.as_dict()
