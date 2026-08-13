"""CORS 只放行 extension 與 loopback。

這個 API 沒有認證 —— CORS 設太寬等於讓任何你造訪的網頁都能讀寫下載歷史。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from snsmediadl.api.app import create_app, get_session


@pytest.fixture()
def client(cfg, session):
    app = create_app(cfg)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


ALLOWED = [
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    "http://127.0.0.1:8765",
    "http://localhost:3000",
    "https://localhost",
]

BLOCKED = [
    "https://evil.example.com",
    "https://x.com",
    "http://192.168.1.50:8765",
    "null",
]


@pytest.mark.parametrize("origin", ALLOWED)
def test_allowed_origins_get_cors_header(client, origin):
    r = client.get("/api/health", headers={"Origin": origin})
    assert r.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize("origin", BLOCKED)
def test_blocked_origins_get_no_cors_header(client, origin):
    r = client.get("/api/health", headers={"Origin": origin})
    assert "access-control-allow-origin" not in r.headers


def test_preflight_allows_post_from_extension(client):
    r = client.options(
        "/api/ingest",
        headers={
            "Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert "POST" in r.headers.get("access-control-allow-methods", "")


def test_preflight_rejected_for_foreign_origin(client):
    r = client.options(
        "/api/ingest",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in r.headers
