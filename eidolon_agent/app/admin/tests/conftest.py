"""The credential every test on this surface has to carry.

The admin routers require this Host's Agent credential — on the routers rather
than on the app factory, so that mounting one on a bare ``FastAPI()`` (which most
of these tests do) exercises the surface that actually ships. That makes every
test here a caller, and a caller needs a token.

Set for the whole package rather than per module: a test that forgot it would
fail with a 401 that reads like the thing under test being broken.
"""

from __future__ import annotations

import httpx
import pytest

from eidolon_agent.app.admin.authority import SERVICE_TOKEN_ENV

ADMIN_TOKEN = "agent-admin-test-token"
AUTHORITY_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture(autouse=True)
def agent_admin_credential(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure the Host's credential for the duration of one test."""

    monkeypatch.setenv(SERVICE_TOKEN_ENV, ADMIN_TOKEN)
    return ADMIN_TOKEN


def authorized_client(app, *, base_url: str = "http://t") -> httpx.AsyncClient:
    """An HTTP client that presents the credential on every request."""

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base_url,
        headers=AUTHORITY_HEADERS,
    )
