"""PairingCoordinator — issue / exchange / expiry / revocation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from eidolon_agent.app.transport.pairing import PairingCoordinator, PairingTokenVerifier
from eidolon_agent.core.errors import NotFoundError, UnauthenticatedError

pytestmark = pytest.mark.unit


SECRET = "test-secret-32chars-minimum-for-hs256-x"


def _coord(**overrides) -> PairingCoordinator:
    return PairingCoordinator(
        jwt_secret=SECRET,
        code_ttl_s=overrides.get("code_ttl_s", 600),
        code_length=overrides.get("code_length", 8),
        token_ttl_days=1,
    )


async def test_issue_code_format() -> None:
    rec = await _coord().issue_code(
        tenant_id="t", user_id="u", default_template_id="tpl", issued_by_actor="admin"
    )
    assert len(rec.code) == 8
    assert rec.expires_at > datetime.now(timezone.utc)
    assert rec.issued_by_actor == "admin"


async def test_exchange_returns_token_for_valid_code() -> None:
    c = _coord()
    rec = await c.issue_code(
        tenant_id="t", user_id="alice", default_template_id="tpl", issued_by_actor="admin"
    )
    issued = await c.exchange(code=rec.code, device_id="dev-1")
    assert issued.device_id == "dev-1"
    assert issued.token  # non-empty JWT
    assert issued.tenant_id == "t"
    assert issued.user_id == "alice"


async def test_exchange_unknown_code_raises_not_found() -> None:
    c = _coord()
    with pytest.raises(NotFoundError):
        await c.exchange(code="GHOSTCDE", device_id=None)


async def test_exchange_is_single_use() -> None:
    c = _coord()
    rec = await c.issue_code(
        tenant_id="t", user_id="u", default_template_id=None, issued_by_actor="admin"
    )
    await c.exchange(code=rec.code, device_id=None)
    with pytest.raises(NotFoundError):
        await c.exchange(code=rec.code, device_id=None)  # second use rejected


async def test_exchange_expired_code_raises_unauthenticated() -> None:
    c = _coord(code_ttl_s=0)  # already expired by the time we exchange
    rec = await c.issue_code(
        tenant_id="t", user_id="u", default_template_id=None, issued_by_actor="admin"
    )
    # Force expiry deterministically.
    rec_expired = type(rec)(
        code=rec.code,
        tenant_id=rec.tenant_id,
        user_id=rec.user_id,
        default_template_id=rec.default_template_id,
        issued_at=rec.issued_at,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        issued_by_actor=rec.issued_by_actor,
    )
    c._codes[rec.code] = rec_expired  # type: ignore[attr-defined]
    with pytest.raises(UnauthenticatedError):
        await c.exchange(code=rec.code, device_id=None)


async def test_token_verifier_round_trip() -> None:
    c = _coord()
    rec = await c.issue_code(
        tenant_id="t", user_id="alice", default_template_id="tpl", issued_by_actor="admin"
    )
    issued = await c.exchange(code=rec.code, device_id="dev-x")

    verifier = PairingTokenVerifier(secret=SECRET, algorithm="HS256")
    device = await verifier.verify(issued.token)
    assert device.device_id == "dev-x"
    assert device.tenant_id == "t"
    assert device.user_id == "alice"


async def test_token_verifier_rejects_wrong_secret() -> None:
    c = _coord()
    rec = await c.issue_code(
        tenant_id="t", user_id="u", default_template_id=None, issued_by_actor="admin"
    )
    issued = await c.exchange(code=rec.code, device_id=None)
    bad = PairingTokenVerifier(secret="some-other-32+chars-different-secret-x", algorithm="HS256")
    with pytest.raises(UnauthenticatedError):
        await bad.verify(issued.token)
