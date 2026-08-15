from __future__ import annotations

import json
import pathlib

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from snsmediadl.config import Config
from snsmediadl.db.session import make_engine
from snsmediadl.db.models import Base

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """測試一律不打網路。

    「下載層用 fixture 測」是驗收條件之一，但這種保證不強制就會慢慢腐爛 ——
    有人加了一個忘記塞 MockTransport 的測試，套件就開始依賴外部服務與網路狀態。
    MockTransport 與 ASGITransport 走的是不同的類別，不受影響。
    """

    def blocked(*_args, **_kwargs):
        raise RuntimeError(
            "測試嘗試發出真實網路請求。請改用 httpx.MockTransport。"
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)


@pytest.fixture(autouse=True)
def _clean_process_state():
    """清掉跨測試殘留的**行程層級**狀態。

    `services.ingest` 記著「extension 最後一次送進來什麼」給 GUI 的背景活動區
    看。它不歸任何一個 session 管，所以會跨測試留下來 —— 症狀是「還沒收到過」
    這個初始狀態只有在單獨跑那個測試時才成立，整套一起跑就失敗。
    """
    from snsmediadl.services.ingest import reset_last_ingest

    reset_last_ingest()
    yield
    reset_last_ingest()


@pytest.fixture()
def engine():
    eng = make_engine(url="sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine) -> Session:
    maker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with maker() as s:
        yield s


@pytest.fixture()
def cfg(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "test.db",
        output_root=tmp_path / "out",
        concurrency=2,
        max_attempts=2,
        # 測試不需要真的等 —— 節流本身另有專門的計時測試
        download_delay_seconds=0.0,
        fetch_account_delay_seconds=0.0,
    )


@pytest.fixture()
def sample_account() -> list[dict]:
    """spike 實測捕獲的真實資料：4 posts / 6 media，含混合型別貼文。"""
    return json.loads((FIXTURES / "sample_account.json").read_text(encoding="utf-8"))
