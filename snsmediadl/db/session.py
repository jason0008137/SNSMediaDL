"""Engine / Session 建立。

SQLite 需要兩件事手動開：
  - foreign_keys：預設是關的，FK 不會被強制
  - WAL：讓 worker 寫入時 API 仍可讀
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..config import Config, load_config
from .models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


def make_engine(cfg: Config | None = None, url: str | None = None) -> Engine:
    cfg = cfg or load_config()
    target = url or cfg.db_url

    # 下載 worker 的 DB 操作跑在 asyncio.to_thread 的其他執行緒，
    # 所以連線必須允許跨執行緒使用。
    kwargs: dict = {"connect_args": {"check_same_thread": False}}

    if ":memory:" in target:
        # in-memory SQLite 是「每條連線一個獨立資料庫」。不釘成單一連線的話，
        # 其他執行緒會看到一個空的 DB。
        kwargs["poolclass"] = StaticPool
    elif target.startswith("sqlite:///"):
        cfg.db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(target, future=True, **kwargs)
    event.listen(engine, "connect", _configure_sqlite)
    return engine


def init_engine(cfg: Config | None = None, url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    _engine = make_engine(cfg, url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def create_all(engine: Engine | None = None) -> None:
    """建表。正式流程走 alembic，這個給測試與首次啟動用。"""
    Base.metadata.create_all(engine or get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
