"""Phase 33.B1: user-level token revocation propagates to verifier.

Two layers under test:

1. Endpoint: ``POST /api/admin/users/{user_id}/revoke-sessions`` writes
   ``revoked.user.<user_id>`` to the DEVICE_REVOCATIONS KV.
2. Verifier: ``PairingTokenVerifier.verify`` checks this key on every
   call and raises ``RuntimeTokenRevokedError`` when present — regardless of
   how recently the token was minted.
"""

from __future__ import annotations

import httpx
import pytest
from eidolon_data import DataSettings, DataStore
from eidolon_data.schema.models import (
    CompanionRow as DataCompanionRow,
)
from eidolon_data.schema.models import (
    ConversationRow as DataConversationRow,
)
from eidolon_data.schema.models import (
    DeviceRow as DataDeviceRow,
)
from eidolon_data.schema.models import (
    EventRow as DataEventRow,
)
from eidolon_data.schema.models import (
    JobRow as DataJobRow,
)
from eidolon_data.schema.models import (
    MemoryRealmRow as DataMemoryRealmRow,
)
from eidolon_data.schema.models import (
    MessageRow as DataMessageRow,
)
from eidolon_data.schema.models import (
    PersonaGenomeRow as DataPersonaGenomeRow,
)
from eidolon_data.schema.models import (
    TurnRow as DataTurnRow,
)
from eidolon_sdk.biz.runtime import (
    PairingTokenVerifier,
    RuntimeTokenRevokedError,
    device_revocation_keys,
    sign_device_token,
    user_revocation_keys,
)
from fastapi import FastAPI
from sqlalchemy import func, select

from eidolon_agent.app.admin.routers import devices as devices_router

pytestmark = pytest.mark.functional

SECRET = "test-secret-32-bytes-long-aaaaaaaaa"


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
    app.include_router(devices_router.router, prefix="/api/admin")
    app.state.revocation_kv = kv
    return app


# ---- endpoint ------------------------------------------------------------


async def test_revoke_user_sessions_writes_revocation_key() -> None:
    """``POST /api/admin/users/manson/revoke-sessions`` writes a key
    that the verifier (later) will treat as 'all manson sessions are
    revoked'."""
    kv = _FakeKV()
    app = _build_test_app(kv)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/admin/users/manson/revoke-sessions")

    assert r.status_code == 200
    body = r.json()
    assert body == {"user_id": "manson", "revoked": True}
    # Key written with a non-empty value (ISO timestamp). The endpoint writes
    # both the encoded key and legacy simple-id key for compatibility.
    assert await kv.get(user_revocation_keys("manson")[0]) is not None
    val = await kv.get("revoked.user.manson")
    assert val is not None
    assert b"T" in val and b":" in val  # ISO format roughly


async def test_delete_user_data_503_when_data_store_missing() -> None:
    kv = _FakeKV()
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.delete("/api/admin/users/alice/data")

    assert r.status_code == 503
    assert "data_store" in r.json()["detail"]


async def test_delete_user_data_prefers_eidolon_data_store(tmp_path) -> None:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    kv = _FakeKV()
    await kv.put(user_revocation_keys("alice")[0], b"revoked")
    await kv.put("revoked.user.alice", b"revoked")

    try:
        await store.owners.create(owner_id="alice", display_name="Alice")
        await store.companions.create(companion_id="companion-a", owner_id="alice")
        await store.persona_repo.create_genome(
            genome_id="genome-a",
            companion_id="companion-a",
            version=1,
            genome_json={"persona_instance": {"instance_id": "companion-a"}},
        )
        await store.companions.set_current_genome("companion-a", "genome-a")
        await store.devices.create_device(
            device_id="dev-a",
            owner_id="alice",
            bound_companion_id="companion-a",
            auth_type="token",
            secret_ref="secret-ref",
            access_policy_json={"capability": "chat"},
        )
        await store.conversations.create_conversation(
            conversation_id="conv-a",
            owner_id="alice",
            companion_id="companion-a",
            device_id="dev-a",
        )
        await store.conversations.append_turn(
            turn_id="turn-a",
            conversation_id="conv-a",
            seq=1,
        )
        await store.conversations.append_message(
            message_id="msg-a",
            turn_id="turn-a",
            role="user",
            content="hello",
        )
        await store.jobs.create(
            job_id="job-a",
            owner_id="alice",
            provider="mementos",
            kind="writing",
            turn_id="turn-a",
        )
        await store.memory_repo.create_realm(
            realm_id="realm-a",
            owner_id="alice",
            companion_id="companion-a",
        )
        await store.events.append(
            event_id="evt-a",
            owner_id="alice",
            subject_type="persona",
            subject_id="companion-a",
            event_type="persona.evolution.applied",
        )

        app = _build_test_app(kv)
        app.state.data_store = store
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.delete("/api/admin/users/alice/data")

        assert r.status_code == 200
        body = r.json()
        assert body["deleted"] is True
        assert body["counts"]["conversations"] == 1
        assert body["counts"]["turns"] == 1
        assert body["counts"]["messages"] == 1
        assert body["counts"]["persona_genomes"] == 1
        assert body["counts"]["memory_realms"] == 1
        assert body["counts"]["jobs"] == 1
        assert body["counts"]["devices"] == 1
        assert body["counts"]["events"] == 1
        assert body["revocation_keys_cleared"] == 2

        async with store.session_factory() as session:
            for model in (
                DataConversationRow,
                DataTurnRow,
                DataMessageRow,
                DataPersonaGenomeRow,
                DataMemoryRealmRow,
                DataJobRow,
                DataDeviceRow,
                DataEventRow,
            ):
                count = await session.scalar(select(func.count()).select_from(model))
                assert count == 0
            companion = await session.get(DataCompanionRow, "companion-a")
            assert companion is not None
            assert companion.status == "deleted"
            assert companion.current_genome_id is None
    finally:
        await store.close()


async def test_revoke_user_sessions_503_when_kv_missing() -> None:
    """If the bucket wasn't initialized at startup (NATS down at boot,
    say), the endpoint must report 503 — not silently succeed."""
    app = FastAPI()
    app.include_router(devices_router.router, prefix="/api/admin")
    # NOT setting app.state.revocation_kv on purpose

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/admin/users/manson/revoke-sessions")

    assert r.status_code == 503
    assert "revocation_kv" in r.json()["detail"]


# ---- verifier integration ------------------------------------------------


async def test_verifier_rejects_token_after_user_revoke() -> None:
    """The whole point of this phase: a freshly-minted, not-expired
    token whose ``user_id`` is in ``revoked.user.<id>`` must fail
    verification with ``RuntimeTokenRevokedError``.

    Mirrors the runtime flow: channel signed a JWT for manson; admin
    operator then revoked manson; next chat() turn that calls
    verifier.verify() should be rejected immediately."""
    kv = _FakeKV()
    verifier = PairingTokenVerifier(secret=SECRET, revocation_kv=kv)

    token, _ = sign_device_token(
        secret=SECRET,
        device_id="web-abc12345",
        tenant_id="default",
        user_id="manson",
        default_template_id="caretaker_jiezhi",
        scopes=["device"],
    )
    # Sanity: works pre-revoke.
    verified = await verifier.verify(token)
    assert verified.user_id == "manson"

    # Operator revokes manson via the endpoint.
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/admin/users/manson/revoke-sessions")
    assert r.status_code == 200

    # Same token now rejected.
    with pytest.raises(RuntimeTokenRevokedError) as exc_info:
        await verifier.verify(token)
    assert "manson" in str(exc_info.value)


async def test_verifier_user_revoke_does_not_affect_other_users() -> None:
    """Revoking manson must not impact default. Sanity that the key
    scoping (revoked.user.<id>) is correctly per-user."""
    kv = _FakeKV()
    verifier = PairingTokenVerifier(secret=SECRET, revocation_kv=kv)

    manson_token, _ = sign_device_token(
        secret=SECRET, device_id="web-1", tenant_id="t", user_id="manson",
        default_template_id=None, scopes=["device"],
    )
    default_token, _ = sign_device_token(
        secret=SECRET, device_id="web-2", tenant_id="t", user_id="default",
        default_template_id=None, scopes=["device"],
    )

    # Revoke just manson via the endpoint.
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post("/api/admin/users/manson/revoke-sessions")

    with pytest.raises(RuntimeTokenRevokedError):
        await verifier.verify(manson_token)
    # default still works.
    verified = await verifier.verify(default_token)
    assert verified.user_id == "default"


async def test_verifier_device_level_revoke_still_works() -> None:
    """Phase 33.B1 added user-level revoke but must NOT regress the
    pre-existing device-level revoke path."""
    kv = _FakeKV()
    verifier = PairingTokenVerifier(secret=SECRET, revocation_kv=kv)

    token, _ = sign_device_token(
        secret=SECRET, device_id="dev-x", tenant_id="t", user_id="alice",
        default_template_id=None, scopes=["device"],
    )

    # Manually write a device-level revocation (no admin endpoint for
    # this scope yet — comes from pairing flow today).
    await kv.put("revoked.dev-x", b"manual-test")

    with pytest.raises(RuntimeTokenRevokedError) as exc_info:
        await verifier.verify(token)
    assert "dev-x" in str(exc_info.value)


async def test_verifier_accepts_mac_device_id_without_invalid_kv_key() -> None:
    """MAC-style ESP32 ids contain ``:`` and must not be used raw as KV keys."""
    kv = _FakeKV()
    verifier = PairingTokenVerifier(secret=SECRET, revocation_kv=kv)

    token, _ = sign_device_token(
        secret=SECRET,
        device_id="1c:db:d4:7a:ef:0c",
        tenant_id="t",
        user_id="alice",
        default_template_id=None,
        scopes=["device"],
    )

    verified = await verifier.verify(token)
    assert verified.device_id == "1c:db:d4:7a:ef:0c"


async def test_verifier_rejects_mac_device_id_with_encoded_revocation_key() -> None:
    kv = _FakeKV()
    verifier = PairingTokenVerifier(secret=SECRET, revocation_kv=kv)
    device_id = "1c:db:d4:7a:ef:0c"

    token, _ = sign_device_token(
        secret=SECRET,
        device_id=device_id,
        tenant_id="t",
        user_id="alice",
        default_template_id=None,
        scopes=["device"],
    )
    await kv.put(device_revocation_keys(device_id)[0], b"manual-test")

    with pytest.raises(RuntimeTokenRevokedError) as exc_info:
        await verifier.verify(token)
    assert device_id in str(exc_info.value)
