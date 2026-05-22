"""Admin /api/admin/pairing/codes — issue code via TestClient."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eidolon_agent.app.admin.routers import pairing as pairing_router
from eidolon_agent.app.transport.pairing import PairingCoordinator

pytestmark = pytest.mark.functional


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(pairing_router.router, prefix="/api/admin")
    app.state.pairing = PairingCoordinator(jwt_secret="test-secret-not-for-prod-x" * 2)
    return TestClient(app)


def test_issue_code_returns_8_char_alphanumeric(client: TestClient) -> None:
    r = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "alice", "default_template_id": "tpl"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["code"]) == 8
    assert body["pair_url"].startswith("eidolon://pair?code=")
    assert body["pair_url"].endswith(body["code"])


def test_issue_code_without_template_is_optional(client: TestClient) -> None:
    r = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "u"},
    )
    assert r.status_code == 200


def test_qr_endpoint_returns_png(client: TestClient) -> None:
    issued = client.post(
        "/api/admin/pairing/codes",
        json={"tenant_id": "t", "user_id": "u"},
    ).json()
    r = client.get(f"/api/admin/pairing/codes/{issued['code']}.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic
