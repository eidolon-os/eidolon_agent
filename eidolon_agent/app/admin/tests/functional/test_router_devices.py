"""Admin /api/admin/devices rotation endpoint."""

from __future__ import annotations

import httpx
import pytest
from eidolon_sdk.biz.runtime import PairingTokenVerifier
from fastapi import FastAPI

from eidolon_agent.app.admin.routers import devices as devices_router
from eidolon_agent.app.transport.pairing import PairingCoordinator

pytestmark = pytest.mark.functional

SECRET = "test-secret-not-for-prod-x" * 2


async def _client_with_token(device_id: str = "dev-1") -> tuple[httpx.AsyncClient, str]:
    app = FastAPI()
    app.include_router(devices_router.router, prefix="/api/admin")
    pairing = PairingCoordinator(jwt_secret=SECRET)
    verifier = PairingTokenVerifier(secret=SECRET)
    app.state.pairing = pairing
    app.state.pairing_verifier = verifier

    rec = await pairing.issue_code(
        tenant_id="t",
        user_id="alice",
        default_template_id="tpl",
        issued_by_actor="test",
    )
    issued = await pairing.exchange(code=rec.code, device_id=device_id)

    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), issued.token


async def test_rotate_device_token_returns_new_token_for_same_device() -> None:
    client, token = await _client_with_token()

    async with client:
        r = await client.post(
            "/api/admin/devices/dev-1/rotate",
            headers={"authorization": f"Bearer {token}"},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "dev-1"
    assert body["device_token"] != token

    verifier = PairingTokenVerifier(secret=SECRET)
    verified = await verifier.verify(body["device_token"])
    assert verified.device_id == "dev-1"
    assert verified.tenant_id == "t"
    assert verified.user_id == "alice"
    assert verified.default_template_id == "tpl"


async def test_rotate_rejects_missing_bearer_token() -> None:
    client, _token = await _client_with_token()

    async with client:
        r = await client.post("/api/admin/devices/dev-1/rotate")

    assert r.status_code == 401


async def test_rotate_rejects_mismatched_device_id() -> None:
    client, token = await _client_with_token(device_id="dev-1")

    async with client:
        r = await client.post(
            "/api/admin/devices/dev-2/rotate",
            headers={"authorization": f"Bearer {token}"},
        )

    assert r.status_code == 403
