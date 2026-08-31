"""Owner-level token revocation propagates to verifier.

Two layers under test:

1. Endpoint: ``POST /api/admin/owners/{owner_id}/revoke-sessions`` writes
   owner revocation keys to the DEVICE_REVOCATIONS KV.
2. Verifier: ``RuntimeTokenVerifier.verify`` reads the instant stored there and
   refuses tokens issued **before** it. A token minted afterwards works, which
   is what makes "sign every device out" recoverable: the devices come back with
   a fresh token instead of being locked out of the namespace for good.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from eidolon_sdk.biz.runtime import (
    RuntimeTokenRevokedError,
    RuntimeTokenVerifier,
    device_revocation_keys,
    owner_revocation_keys,
    sign_runtime_token,
)
from fastapi import FastAPI

from eidolon_agent.app.admin.routers import owner_runtime as owner_runtime_router
from eidolon_agent.app.admin.tests.conftest import AUTHORITY_HEADERS
from eidolon_agent.infra.persistence.runtime_store import (
    AgentRuntimeStore,
    ConversationRow,
    JobRow,
    MessageRow,
    RuntimeSessionRow,
    TurnRow,
)

pytestmark = pytest.mark.functional

SECRET = "test-secret-32-bytes-long-aaaaaaaaa"


def _sign_device_token(*, device_id: str, **kwargs):
    return sign_runtime_token(
        secret=SECRET,
        device_id=device_id,
        session_id=kwargs.pop("session_id", "test-session"),
        ttl_seconds=kwargs.pop("ttl_seconds", 3600),
        **kwargs,
    )


class _FakeKV:
    """Minimal in-memory KV stand-in for tests. The real implementation
    is a ``NatsKVStore`` (eidolon_agent/infra/nats/...) — same surface,
    we don't need its async networking here."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        if ":" in key:
            raise AssertionError(f"raw unsafe KV key used: {key}")
        return self._store.get(key)

    async def put(self, key: str, value: bytes) -> None:
        if ":" in key:
            raise AssertionError(f"raw unsafe KV key used: {key}")
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)


def _build_test_app(kv: _FakeKV) -> FastAPI:
    app = FastAPI()
    app.include_router(owner_runtime_router.router, prefix="/api/admin")
    app.state.revocation_kv = kv
    return app


# ---- endpoint ------------------------------------------------------------


async def test_revoke_owner_sessions_writes_revocation_key() -> None:
    kv = _FakeKV()
    app = _build_test_app(kv)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        r = await client.post("/api/admin/owners/manson/revoke-sessions")

    assert r.status_code == 200
    body = r.json()
    assert body["owner_id"] == "manson"
    assert body["revoked"] is True
    val = await kv.get(owner_revocation_keys("manson")[0])
    assert val is not None
    # The stored value is the watermark itself: the verifier refuses tokens
    # issued before this instant and accepts ones issued after, which is what
    # makes signing every device out recoverable rather than a lockout.
    assert val.decode("utf-8") == body["revoked_at"]
    assert datetime.fromisoformat(body["revoked_at"]).tzinfo is not None


async def test_delete_owner_data_503_when_runtime_store_missing() -> None:
    kv = _FakeKV()
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        r = await client.delete("/api/admin/owners/alice/data")

    assert r.status_code == 503
    assert "runtime_store" in r.json()["detail"]


async def test_delete_owner_data_prefers_agent_runtime_authority(tmp_path) -> None:
    store = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await store.init_schema()
    kv = _FakeKV()
    try:
        async with store.session_factory() as session:
            session.add(
                RuntimeSessionRow(
                    session_id="session-a",
                    owner_id="alice",
                    companion_id="companion-a",
                )
            )
            await session.flush()
            session.add(
                ConversationRow(
                    conversation_id="conv-a",
                    owner_id="alice",
                    companion_id="companion-a",
                    runtime_session_id="session-a",
                )
            )
            await session.flush()
            session.add(TurnRow(turn_id="turn-a", conversation_id="conv-a", seq=0))
            await session.flush()
            session.add(
                MessageRow(
                    message_id="message-a",
                    turn_id="turn-a",
                    seq=0,
                    role="user",
                    content="hello",
                )
            )
            session.add(
                JobRow(
                    job_id="job-a",
                    owner_id="alice",
                    companion_id="companion-a",
                    provider="mementos",
                    kind="writing",
                )
            )
            await session.commit()

        app = _build_test_app(kv)
        app.state.runtime_store = store
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=AUTHORITY_HEADERS,
        ) as client:
            response = await client.delete("/api/admin/owners/alice/data")

        assert response.status_code == 200
        assert response.json()["counts"] == {
            "messages": 1,
            "turns": 1,
            "jobs": 1,
            "conversations": 1,
            "runtime_sessions": 1,
            "memory_turn_outbox": 0,
        }
        assert response.json()["revocation_keys_written"] == 1
        assert await kv.get(owner_revocation_keys("alice")[0]) is not None
    finally:
        await store.close()


async def test_revoke_owner_sessions_503_when_kv_missing() -> None:
    """If the bucket wasn't initialized at startup (NATS down at boot,
    say), the endpoint must report 503 — not silently succeed."""
    app = FastAPI()
    app.include_router(owner_runtime_router.router, prefix="/api/admin")
    # NOT setting app.state.revocation_kv on purpose

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        r = await client.post("/api/admin/owners/manson/revoke-sessions")

    assert r.status_code == 503
    assert "revocation_kv" in r.json()["detail"]


# ---- verifier integration ------------------------------------------------


async def test_verifier_rejects_token_after_owner_revoke() -> None:
    kv = _FakeKV()
    verifier = RuntimeTokenVerifier(secret=SECRET, revocation_kv=kv)

    token, _ = _sign_device_token(
        device_id="web-abc12345",
        owner_id="manson",
        companion_id="companion-a",
        scopes=["device"],
    )
    # Sanity: works pre-revoke.
    verified = await verifier.verify(token)
    assert verified.owner_id == "manson"

    # Operator revokes manson via the endpoint.
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        r = await client.post("/api/admin/owners/manson/revoke-sessions")
    assert r.status_code == 200

    # Same token now rejected.
    with pytest.raises(RuntimeTokenRevokedError) as exc_info:
        await verifier.verify(token)
    assert "manson" in str(exc_info.value)


async def test_verifier_owner_revoke_does_not_affect_other_owners() -> None:
    kv = _FakeKV()
    verifier = RuntimeTokenVerifier(secret=SECRET, revocation_kv=kv)

    manson_token, _ = _sign_device_token(
        device_id="web-1",
        owner_id="manson",
        companion_id="companion-a",
        scopes=["device"],
    )
    default_token, _ = _sign_device_token(
        device_id="web-2",
        owner_id="default",
        companion_id="companion-b",
        scopes=["device"],
    )

    # Revoke just manson via the endpoint.
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        await client.post("/api/admin/owners/manson/revoke-sessions")

    with pytest.raises(RuntimeTokenRevokedError):
        await verifier.verify(manson_token)
    # default still works.
    verified = await verifier.verify(default_token)
    assert verified.owner_id == "default"


async def test_verifier_device_level_revoke_still_works() -> None:
    """Phase 33.B1 added user-level revoke but must NOT regress the
    pre-existing device-level revoke path."""
    kv = _FakeKV()
    verifier = RuntimeTokenVerifier(secret=SECRET, revocation_kv=kv)

    token, _ = _sign_device_token(
        device_id="dev-x",
        owner_id="alice",
        companion_id="companion-a",
        scopes=["device"],
    )

    await kv.put(device_revocation_keys("dev-x")[0], b"manual-test")

    with pytest.raises(RuntimeTokenRevokedError) as exc_info:
        await verifier.verify(token)
    assert "dev-x" in str(exc_info.value)


async def test_verifier_accepts_mac_device_id_without_invalid_kv_key() -> None:
    """MAC-style ESP32 ids contain ``:`` and must not be used raw as KV keys."""
    kv = _FakeKV()
    verifier = RuntimeTokenVerifier(secret=SECRET, revocation_kv=kv)

    token, _ = _sign_device_token(
        device_id="1c:db:d4:7a:ef:0c",
        owner_id="alice",
        companion_id="companion-a",
        scopes=["device"],
    )

    verified = await verifier.verify(token)
    assert verified.device_id == "1c:db:d4:7a:ef:0c"


async def test_verifier_rejects_mac_device_id_with_encoded_revocation_key() -> None:
    kv = _FakeKV()
    verifier = RuntimeTokenVerifier(secret=SECRET, revocation_kv=kv)
    device_id = "1c:db:d4:7a:ef:0c"

    token, _ = _sign_device_token(
        device_id=device_id,
        owner_id="alice",
        companion_id="companion-a",
        scopes=["device"],
    )
    await kv.put(device_revocation_keys(device_id)[0], b"manual-test")

    with pytest.raises(RuntimeTokenRevokedError) as exc_info:
        await verifier.verify(token)
    assert device_id in str(exc_info.value)


async def test_a_token_minted_after_the_revoke_works_again() -> None:
    """The half that makes this offerable to a person.

    Waiting a second is the point rather than an accident: ``iat`` is a whole
    number of seconds and the mark carries microseconds, so a token minted in the
    *same* second as the revoke is refused. That is the safe side to err on — a
    device retries — and this test pins which side it is.
    """

    import asyncio

    kv = _FakeKV()
    verifier = RuntimeTokenVerifier(secret=SECRET, revocation_kv=kv)
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTHORITY_HEADERS
    ) as client:
        revoked = await client.post("/api/admin/owners/manson/revoke-sessions")
    assert revoked.status_code == 200

    same_second, _ = _sign_device_token(
        device_id="web-abc12345",
        owner_id="manson",
        companion_id="companion-a",
        scopes=["device"],
    )
    with pytest.raises(RuntimeTokenRevokedError):
        await verifier.verify(same_second)

    await asyncio.sleep(1.1)
    afterwards, _ = _sign_device_token(
        device_id="web-abc12345",
        owner_id="manson",
        companion_id="companion-a",
        scopes=["device"],
    )

    identity = await verifier.verify(afterwards)

    assert identity.owner_id == "manson"
