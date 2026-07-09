"""Owner-level token revocation propagates to verifier.

Two layers under test:

1. Endpoint: ``POST /api/admin/owners/{owner_id}/revoke-sessions`` writes
   owner revocation keys to the DEVICE_REVOCATIONS KV.
2. Verifier: ``RuntimeTokenVerifier.verify`` checks this key on every
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
    RuntimeTokenRevokedError,
    RuntimeTokenVerifier,
    device_revocation_keys,
    owner_revocation_keys,
    sign_runtime_token,
)
from fastapi import FastAPI
from sqlalchemy import func, select

from eidolon_agent.app.admin.routers import devices as devices_router

pytestmark = pytest.mark.functional

SECRET = "test-secret-32-bytes-long-aaaaaaaaa"


def _sign_device_actor_token(*, device_id: str, **kwargs):
    return sign_runtime_token(
        secret=SECRET,
        actor_kind="device",
        actor_id=device_id,
        device_id=device_id,
        schema_version=kwargs.pop("schema_version", "eidolon.persona_genome.v1"),
        genome_hash=kwargs.pop("genome_hash", "pgv1_revoke_test"),
        compiler_version=kwargs.pop("compiler_version", "eidolon.persona_compiler.v1"),
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
    app.include_router(devices_router.router, prefix="/api/admin")
    app.state.revocation_kv = kv
    return app


# ---- endpoint ------------------------------------------------------------


async def test_revoke_owner_sessions_writes_revocation_key() -> None:
    kv = _FakeKV()
    app = _build_test_app(kv)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/admin/owners/manson/revoke-sessions")

    assert r.status_code == 200
    body = r.json()
    assert body == {"owner_id": "manson", "revoked": True}
    val = await kv.get(owner_revocation_keys("manson")[0])
    assert val is not None
    assert b"T" in val and b":" in val  # ISO format roughly


async def test_delete_owner_data_503_when_data_store_missing() -> None:
    kv = _FakeKV()
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.delete("/api/admin/owners/alice/data")

    assert r.status_code == 503
    assert "data_store" in r.json()["detail"]


async def test_delete_owner_data_prefers_eidolon_data_store(tmp_path) -> None:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    kv = _FakeKV()
    await kv.put(owner_revocation_keys("alice")[0], b"revoked")

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
            source_device_id="dev-a",
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
            event_id="evt_a",
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
            r = await client.delete("/api/admin/owners/alice/data")

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
        assert body["revocation_keys_cleared"] == 1

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


async def test_revoke_owner_sessions_503_when_kv_missing() -> None:
    """If the bucket wasn't initialized at startup (NATS down at boot,
    say), the endpoint must report 503 — not silently succeed."""
    app = FastAPI()
    app.include_router(devices_router.router, prefix="/api/admin")
    # NOT setting app.state.revocation_kv on purpose

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post("/api/admin/owners/manson/revoke-sessions")

    assert r.status_code == 503
    assert "revocation_kv" in r.json()["detail"]


# ---- verifier integration ------------------------------------------------


async def test_verifier_rejects_token_after_owner_revoke() -> None:
    kv = _FakeKV()
    verifier = RuntimeTokenVerifier(secret=SECRET, revocation_kv=kv)

    token, _ = _sign_device_actor_token(
        device_id="web-abc12345",
        owner_id="manson",
        companion_id="companion-a",
        memory_realm_id="realm-a",
        genome_id="genome-a",
        scopes=["device"],
    )
    # Sanity: works pre-revoke.
    verified = await verifier.verify(token)
    assert verified.owner_id == "manson"

    # Operator revokes manson via the endpoint.
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
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

    manson_token, _ = _sign_device_actor_token(
        device_id="web-1", owner_id="manson",
        companion_id="companion-a", memory_realm_id="realm-a", genome_id="genome-a",
        scopes=["device"],
    )
    default_token, _ = _sign_device_actor_token(
        device_id="web-2", owner_id="default",
        companion_id="companion-b", memory_realm_id="realm-b", genome_id="genome-b",
        scopes=["device"],
    )

    # Revoke just manson via the endpoint.
    app = _build_test_app(kv)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
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

    token, _ = _sign_device_actor_token(
        device_id="dev-x", owner_id="alice",
        companion_id="companion-a", memory_realm_id="realm-a", genome_id="genome-a",
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

    token, _ = _sign_device_actor_token(
        device_id="1c:db:d4:7a:ef:0c",
        owner_id="alice",
        companion_id="companion-a",
        memory_realm_id="realm-a",
        genome_id="genome-a",
        scopes=["device"],
    )

    verified = await verifier.verify(token)
    assert verified.device_id == "1c:db:d4:7a:ef:0c"


async def test_verifier_rejects_mac_device_id_with_encoded_revocation_key() -> None:
    kv = _FakeKV()
    verifier = RuntimeTokenVerifier(secret=SECRET, revocation_kv=kv)
    device_id = "1c:db:d4:7a:ef:0c"

    token, _ = _sign_device_actor_token(
        device_id=device_id,
        owner_id="alice",
        companion_id="companion-a",
        memory_realm_id="realm-a",
        genome_id="genome-a",
        scopes=["device"],
    )
    await kv.put(device_revocation_keys(device_id)[0], b"manual-test")

    with pytest.raises(RuntimeTokenRevokedError) as exc_info:
        await verifier.verify(token)
    assert device_id in str(exc_info.value)
