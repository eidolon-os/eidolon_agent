"""Protocol-level E2E for a dynamic cross-Companion device capability.

The test uses the real registration signature verification, SQLite registry,
Agent capability assembly, Hub runtime authorization, command persistence and
terminal-result polling. Only LiveKit transport and the physical Guard speaker
are replaced by an in-memory device simulator.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# This is deliberately a cross-repository acceptance test. Make the sibling
# source trees explicit so it always exercises the checked-out implementation,
# not an older installed wheel.
WORKSPACE = Path(__file__).resolve().parents[3]
for repository in ("eidolon_agent", "eidolon_sdk", "eidolon_data", "eidolon_hub"):
    source_root = WORKSPACE / repository
    if not source_root.is_dir():
        raise RuntimeError(f"required sibling repository is missing: {source_root}")
    sys.path.insert(0, str(source_root))

from eidolon_data import DataSettings, DataStore  # noqa: E402
from eidolon_data.adapters import EidolonDataDeviceRegistryRepository  # noqa: E402
from eidolon_sdk.biz.body import CapabilityManifest  # noqa: E402
from eidolon_sdk.biz.devices import (  # noqa: E402
    body_sha256_hex,
    canonical_request,
)
from fastapi import FastAPI  # noqa: E402
from hub.api.routers.runtime import runtime_commands_router  # noqa: E402
from hub.api.routers.system import config_router  # noqa: E402
from hub.config import AppConfig, LiveKitConfig  # noqa: E402
from hub.core.admin_runtime import LiveKitAdminRuntime  # noqa: E402
from hub.core.device_manager import DeviceManager  # noqa: E402
from hub.core.nats_kv import HubNatsKVBucket  # noqa: E402
from hub.core.runtime_blackboard import OwnerRuntimeBlackboard  # noqa: E402

from eidolon_agent.core.ports.tool import ToolInvocationContext  # noqa: E402
from eidolon_agent.core.types.identity import (  # noqa: E402
    CallerContext,
    CallerKind,
    Identity,
)
from eidolon_agent.core.types.tool import ToolCall  # noqa: E402
from eidolon_agent.domain.body_control.adapters import (  # noqa: E402
    HubBodyCommandClient,
    NatsRuntimeBodyDeviceStore,
)
from eidolon_agent.domain.body_control.service import BodyControlService  # noqa: E402
from eidolon_agent.domain.tools.body_capability_provider import (  # noqa: E402
    RuntimeCapabilityToolProvider,
)
from eidolon_agent.infra.events.adapters.inmem import InMemoryKVStore  # noqa: E402

OWNER_A = "owner-1"
OWNER_B = "owner-2"
COMPANION_A = "companion-a"
COMPANION_B = "companion-guard"
BOX_DEVICE = "box-3"
GUARD_DEVICE = "atk-guard"
CAPABILITY_TOOL = "cap_device_roll_call_v1"
GUARD_COMPANION_NAME = "Guard"
SERVICE_TOKEN = "cross-companion-e2e-token"
_DEVICE_KEYS: dict[str, ec.EllipticCurvePrivateKey] = {}


class _FakeRoomService:
    """LiveKit management surface plus a minimal Guard protocol simulator."""

    def __init__(self, *, guard_behavior: str) -> None:
        self.guard_behavior = guard_behavior
        self.runtime: LiveKitAdminRuntime | None = None
        self.sent_envelopes: list[dict] = []
        self.tasks: list[asyncio.Task] = []
        self.registration_ids: dict[str, str] = {}
        self.guard_participant_metadata: dict[str, str] = {}

    async def list_rooms(self, _request):
        # A real Box leaves its control room while a voice session is active.
        rooms = [SimpleNamespace(name="box-voice")]
        if self.guard_behavior != "offline":
            rooms.append(SimpleNamespace(name="guard-control"))
        return SimpleNamespace(rooms=rooms)

    async def list_participants(self, request):
        participants = {
            "box-voice": [
                SimpleNamespace(
                    identity=BOX_DEVICE,
                    sid="PA_BOX",
                    metadata=json.dumps(
                        {
                            "kind": "device",
                            "registration_id": self.registration_ids.get(BOX_DEVICE, ""),
                        }
                    ),
                )
            ],
            "guard-control": [
                SimpleNamespace(
                    identity=GUARD_DEVICE,
                    sid="PA_GUARD",
                    metadata=json.dumps(self.guard_participant_metadata),
                )
            ],
        }
        return SimpleNamespace(participants=participants.get(request.room, []))

    async def send_data(self, request):
        envelope = json.loads(bytes(request.data).decode("utf-8"))
        self.sent_envelopes.append(envelope)
        if envelope.get("dst", {}).get("id") != GUARD_DEVICE:
            return SimpleNamespace()
        if self.guard_behavior in {"success", "invalid_result"}:
            self.tasks.append(asyncio.create_task(self._complete_roll_call(envelope)))
        return SimpleNamespace()

    async def _complete_roll_call(self, command: dict) -> None:
        # Yield until LiveKitAdminRuntime has returned from send_data and changed
        # the command from queued to sent, exactly like a real device response.
        await asyncio.sleep(0.02)
        assert self.runtime is not None
        await self.runtime.apply_command_ack(
            {
                "v": 1,
                "kind": "ack",
                "ref": command["id"],
                "device_id": GUARD_DEVICE,
                "op": command["op"],
                "status": "accepted",
                "code": "OK",
            },
            sender_identity=GUARD_DEVICE,
        )
        await asyncio.sleep(0.02)
        await self.runtime.apply_command_ack(
            {
                "v": 1,
                "kind": "result",
                "ref": command["id"],
                "device_id": GUARD_DEVICE,
                "op": command["op"],
                "status": "completed",
                "code": "OK",
                "result": {"played": "yes" if self.guard_behavior == "invalid_result" else True},
            },
            sender_identity=GUARD_DEVICE,
        )

    async def drain(self) -> None:
        if self.tasks:
            await asyncio.gather(*self.tasks)


class _FakeLiveKitAPI:
    def __init__(self, room: _FakeRoomService) -> None:
        self.room = room

    async def aclose(self) -> None:
        return None


class _Stack:
    def __init__(
        self,
        *,
        store: DataStore,
        manager: DeviceManager,
        runtime: LiveKitAdminRuntime,
        room: _FakeRoomService,
        http: httpx.AsyncClient,
        body: BodyControlService,
        provider: RuntimeCapabilityToolProvider,
        registration_statuses: dict[str, str],
    ) -> None:
        self.store = store
        self.manager = manager
        self.runtime = runtime
        self.room = room
        self.http = http
        self.body = body
        self.provider = provider
        self.registration_statuses = registration_statuses

    async def close(self) -> None:
        await self.room.drain()
        await self.http.aclose()
        await self.store.close()


@asynccontextmanager
async def _stack(
    tmp_path: Path,
    *,
    guard_behavior: str = "success",
    visibility: str = "owner",
    guard_owner: str = OWNER_A,
    guard_capabilities: list[dict] | None = None,
):
    _DEVICE_KEYS.clear()
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "cross-companion.sqlite3")))
    await store.init_schema()
    manager = DeviceManager(EidolonDataDeviceRegistryRepository(store))
    await manager.load()

    config = AppConfig()
    config.esp32.livekit_url = "wss://e2e.invalid"
    config.livekit = LiveKitConfig(
        api_url="http://livekit.invalid",
        api_key="e2e-key",
        api_secret="e2e-secret",
    )
    room = _FakeRoomService(guard_behavior=guard_behavior)
    fake_livekit = _FakeLiveKitAPI(room)
    blackboard_kv = InMemoryKVStore("EIDOLON_RUNTIME_DEVICES")
    blackboard = OwnerRuntimeBlackboard(blackboard_kv)
    await blackboard.initialize([])
    await blackboard.mark_ready([])
    runtime = LiveKitAdminRuntime(config, data_store=store, runtime_blackboard=blackboard)
    runtime._build_livekit_api = lambda: fake_livekit  # type: ignore[method-assign]
    room.runtime = runtime

    app = FastAPI()
    app.include_router(config_router)
    app.include_router(runtime_commands_router)
    app.state.config = config
    app.state.data_store = store
    app.state.device_manager = manager
    app.state.admin_runtime = runtime
    app.state.runtime_blackboard = blackboard
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    )

    try:
        initial = {
            BOX_DEVICE: await _register(
                http,
                device_id=BOX_DEVICE,
                manifest={
                    "device": {"name": "ESP BOX-3", "kind": "esp-box-3"},
                    "capabilities": [_capability("device.identify", "Play local identity cue")],
                },
            ),
            GUARD_DEVICE: await _register(
                http,
                device_id=GUARD_DEVICE,
                manifest={
                    "device": {"name": "ATK Guard", "kind": "atk-guard"},
                    "capabilities": guard_capabilities
                    if guard_capabilities is not None
                    else [_capability("device.roll_call", "Play local roll-call response")],
                    "guard": True,
                    "guard_protocol_versions": [1],
                },
            ),
        }

        await store.owners.create(owner_id=OWNER_A, display_name="Owner A")
        if guard_owner != OWNER_A:
            await store.owners.create(owner_id=guard_owner, display_name="Owner B")
        await store.companions.create(
            owner_id=OWNER_A,
            companion_id=COMPANION_A,
            display_name="Box Companion",
        )
        await store.guard_bindings.ensure_guard_companion(
            owner_id=guard_owner,
            companion_id=COMPANION_B,
            display_name="Guard",
        )
        await store.devices.claim_device(
            BOX_DEVICE,
            owner_id=OWNER_A,
            companion_id=COMPANION_A,
            access_policy_json={"capability_visibility": "owner"},
        )
        await store.guard_bindings.claim(
            owner_id=guard_owner,
            device_id=GUARD_DEVICE,
            guard_companion_id=COMPANION_B,
        )
        await store.devices.update_device(
            GUARD_DEVICE,
            access_policy_json={"capability_visibility": visibility},
        )
        await manager.load()
        # The Guard runtime-config endpoint is intentionally available only to
        # an operator-approved hardware identity.
        await manager.approve(GUARD_DEVICE)
        active = {
            BOX_DEVICE: await _register(
                http,
                device_id=BOX_DEVICE,
                manifest={
                    "device": {"name": "ESP BOX-3", "kind": "esp-box-3"},
                    "capabilities": [_capability("device.identify", "Play local identity cue")],
                },
            ),
            GUARD_DEVICE: await _register(
                http,
                device_id=GUARD_DEVICE,
                manifest={
                    "device": {"name": "ATK Guard", "kind": "atk-guard"},
                    "capabilities": guard_capabilities
                    if guard_capabilities is not None
                    else [_capability("device.roll_call", "Play local roll-call response")],
                    "guard": True,
                    "guard_protocol_versions": [1],
                },
            ),
        }
        assert all(item["status"] == "pending_approval" for item in initial.values())
        room.registration_ids = {
            device_id: str(payload["registration_id"]) for device_id, payload in active.items()
        }
        room.guard_participant_metadata = await _guard_runtime_participant_metadata(
            http,
            device_id=GUARD_DEVICE,
        )
        # Reproduce the real Guard lifecycle: the signed manifest has already
        # advanced, while the still-reachable control participant carries a
        # token minted for an older registration. Availability is the join of
        # current signed manifest + current transport identity, not generation
        # equality between those independent observations.
        room.guard_participant_metadata["registration_id"] = "reg-before-manifest-refresh"
        assert (
            room.guard_participant_metadata["registration_id"]
            != (room.registration_ids[GUARD_DEVICE])
        )
        await runtime.run_probe_cycle([BOX_DEVICE, GUARD_DEVICE])

        command_client = HubBodyCommandClient(
            http,
            base_url="http://127.0.0.1",
            service_token=SERVICE_TOKEN,
            timeout_s=0.3,
        )
        body = BodyControlService(
            device_store=NatsRuntimeBodyDeviceStore(blackboard_kv),
            command_port=command_client,
        )
        stack = _Stack(
            store=store,
            manager=manager,
            runtime=runtime,
            room=room,
            http=http,
            body=body,
            provider=RuntimeCapabilityToolProvider(body),
            registration_statuses={key: str(value["status"]) for key, value in active.items()},
        )
        yield stack
    finally:
        await room.drain()
        await http.aclose()
        await store.close()


async def _register(
    http: httpx.AsyncClient,
    *,
    device_id: str,
    manifest: dict,
    tamper_after_signing: bool = False,
) -> dict:
    body = json.dumps(manifest, separators=(",", ":")).encode()
    headers = _signed_headers(device_id=device_id, body=body)
    if tamper_after_signing:
        manifest = {**manifest, "device": {"name": "Tampered", "kind": "atk-guard"}}
        body = json.dumps(manifest, separators=(",", ":")).encode()
    with patch(
        "hub.api.routers.system.config.generate_token",
        return_value=(device_id, "fake-livekit-token"),
    ):
        response = await http.post("/api/device/register", content=body, headers=headers)
    if tamper_after_signing:
        assert response.status_code == 401
        return {"status": "rejected"}
    assert response.is_success, response.text
    payload = response.json()
    assert payload["config"]["identity"] == device_id
    assert payload["registration_id"]
    return payload


async def _guard_runtime_participant_metadata(
    http: httpx.AsyncClient,
    *,
    device_id: str,
) -> dict[str, str]:
    captured: dict[str, str] = {}

    def _generate_token(**kwargs):
        captured.update(kwargs["participant_metadata"])
        return device_id, "fake-guard-control-token"

    with patch("hub.api.routers.system.config.generate_token", side_effect=_generate_token):
        response = await http.get(
            "/api/guard/runtime-config",
            headers=_signed_headers(
                device_id=device_id,
                body=b"",
                method="GET",
                path_query="/api/guard/runtime-config",
            ),
        )
    assert response.is_success, response.text
    assert captured["kind"] == "guard_control"
    return captured


def _capability(name: str, description: str, *, version: int = 1) -> dict:
    return {
        "name": name,
        "version": version,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "result_schema": {
            "type": "object",
            "properties": {"played": {"type": "boolean"}},
            "required": ["played"],
            "additionalProperties": False,
        },
    }


def _signed_headers(
    *,
    device_id: str,
    body: bytes,
    method: str = "POST",
    path_query: str = "/api/device/register",
) -> dict[str, str]:
    key = _DEVICE_KEYS.setdefault(device_id, ec.generate_private_key(ec.SECP256R1()))
    public_der = key.public_key().public_bytes(
        Encoding.DER,
        PublicFormat.SubjectPublicKeyInfo,
    )
    nonce = f"nonce-{device_id}-{uuid4().hex}"
    timestamp = "0"
    signed = canonical_request(
        method=method,
        path_query=path_query,
        device_id=device_id,
        nonce=nonce,
        timestamp=timestamp,
        body_hash=body_sha256_hex(body),
    )
    headers = {
        "X-Device-ID": device_id,
        "X-Device-Nonce": nonce,
        "X-Device-Timestamp": timestamp,
        "X-Device-Signature": _b64url(key.sign(signed, ec.ECDSA(hashes.SHA256()))),
        "X-Device-Public-Key": _b64url(public_der),
    }
    if method == "POST":
        headers["Content-Type"] = "application/json"
    return headers


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _caller() -> CallerContext:
    return CallerContext(
        identity=Identity(
            owner_id=OWNER_A,
            companion_id=COMPANION_A,
            device_id=BOX_DEVICE,
            memory_realm_id="realm-a",
            genome_id="genome-a",
        ),
        caller_kind=CallerKind.LIVEKIT_VOICE,
        trace_id="trace-roll-call",
        request_id="request-roll-call",
        runtime_caller_id="caller-box-3",
        runtime_session_id="session-box-3",
        actor_kind="voice",
        actor_id=BOX_DEVICE,
        display_name="Owner",
        transport="livekit",
    )


async def _tools(stack: _Stack):
    return await stack.provider.assemble(_caller())


def _runtime_request(*, op: str = "device.roll_call") -> dict:
    return {
        "requester_owner_id": OWNER_A,
        "requester_companion_id": COMPANION_A,
        "source_device_id": BOX_DEVICE,
        "runtime_caller_id": "caller-box-3",
        "runtime_session_id": "session-box-3",
        "runtime_trace_id": "trace-box-3",
        "runtime_turn_id": "turn-box-3",
        "runtime_tool_call_id": "tool-box-3",
        "idempotency_key": f"e2e:{op}",
        "op": op,
        "capability_version": 1,
        "payload": {},
        "qos": "result",
        "ttl_ms": 5000,
    }


def _service_headers() -> dict[str, str]:
    return {"X-Eidolon-Service-Token": SERVICE_TOKEN}


@pytest.mark.asyncio
async def test_real_nats_kv_owner_snapshots_are_isolated_and_cleared_on_hub_restart() -> None:
    try:
        kv = await HubNatsKVBucket.connect(
            url="nats://127.0.0.1:4222",
            bucket="EIDOLON_RUNTIME_DEVICES_E2E",
        )
    except Exception as exc:
        pytest.skip(f"local JetStream is unavailable: {exc}")
    try:
        board = OwnerRuntimeBlackboard(kv, epoch="epoch-e2e")
        await board.initialize([OWNER_A, OWNER_B])
        await board.mark_ready([OWNER_A, OWNER_B])
        for owner_id, device_id, companion_id in (
            (OWNER_A, BOX_DEVICE, COMPANION_A),
            (OWNER_B, GUARD_DEVICE, COMPANION_B),
        ):
            await board.register_device_manifest(
                device_id=device_id,
                manifest=CapabilityManifest.model_validate(
                    {"capabilities": [_capability("device.identify", "Identify locally")]}
                ),
                owner_id=owner_id,
                provider_companion_id=companion_id,
                provider_companion_name=(
                    "Box Companion" if companion_id == COMPANION_A else GUARD_COMPANION_NAME
                ),
                name=device_id,
            )
            await board.mark_device_online(
                owner_id=owner_id,
                device_id=device_id,
                room_name=f"room-{device_id}",
                participant_sid=f"PA_{device_id}",
                presence_revision=f"PA_{device_id}",
            )

        reader = NatsRuntimeBodyDeviceStore(kv)
        owner_a_devices = await reader.list_devices(
            owner_id=OWNER_A,
            companion_id=COMPANION_A,
        )
        owner_b_devices = await reader.list_devices(
            owner_id=OWNER_B,
            companion_id=COMPANION_B,
        )
        assert [item.device_id for item in owner_a_devices] == [BOX_DEVICE]
        assert [item.device_id for item in owner_b_devices] == [GUARD_DEVICE]

        restarted = OwnerRuntimeBlackboard(kv, epoch="epoch-restarted")
        await restarted.initialize([OWNER_A, OWNER_B])
        await restarted.mark_ready([OWNER_A, OWNER_B])
        assert (
            await reader.list_devices(
                owner_id=OWNER_A,
                companion_id=COMPANION_A,
            )
            == []
        )
        assert (
            await reader.list_devices(
                owner_id=OWNER_B,
                companion_id=COMPANION_B,
            )
            == []
        )
    finally:
        await kv.close()


@pytest.mark.asyncio
async def test_box_voice_tool_calls_cross_companion_guard_and_waits_for_real_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path) as stack:
        assert stack.registration_statuses == {
            BOX_DEVICE: "pending_approval",
            GUARD_DEVICE: "active",
        }
        guard_row = await stack.store.devices.get_device(GUARD_DEVICE)
        assert guard_row is not None
        assert guard_row.name == "ATK Guard"
        assert guard_row.kind == "atk-guard"
        assert "ops" not in (guard_row.capabilities_json or {})

        schemas, ports = await _tools(stack)
        assert {item.name for item in schemas} == {
            "cap_device_identify_v1",
            CAPABILITY_TOOL,
        }
        assert CAPABILITY_TOOL in ports
        assert ports[CAPABILITY_TOOL].schema.json_schema["properties"]["target_companion"][
            "enum"
        ] == [GUARD_COMPANION_NAME]

        # A repeated signed registration must update the contract atomically
        # without hiding an already-online provider between probe cycles.
        refreshed = await _register(
            stack.http,
            device_id=GUARD_DEVICE,
            manifest={
                "device": {"name": "ATK Guard", "kind": "atk-guard"},
                "capabilities": [_capability("device.roll_call", "Play local roll-call response")],
                "guard": True,
                "guard_protocol_versions": [1],
            },
        )
        assert (
            refreshed["registration_id"] != stack.room.guard_participant_metadata["registration_id"]
        )
        refreshed_schemas, refreshed_ports = await _tools(stack)
        assert CAPABILITY_TOOL in {item.name for item in refreshed_schemas}
        assert CAPABILITY_TOOL in refreshed_ports

        result = await ports[CAPABILITY_TOOL].invoke(
            ToolCall(
                id="tool-call-1",
                name=CAPABILITY_TOOL,
                arguments={
                    "target_companion": GUARD_COMPANION_NAME,
                },
            ),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="turn-guard-in-ma"),
        )
        retry_result = await ports[CAPABILITY_TOOL].invoke(
            ToolCall(
                id="tool-call-retry",
                name=CAPABILITY_TOOL,
                arguments={"target_companion": GUARD_COMPANION_NAME},
            ),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="different-agent-turn"),
        )

        assert result.ok is True
        assert retry_result.ok is True
        assert retry_result.content["command_id"] == result.content["command_id"]
        assert result.content["status"] == "done"
        assert result.content["result"] == {"played": True}
        assert len(stack.room.sent_envelopes) == 1
        envelope = stack.room.sent_envelopes[0]
        assert envelope["op"] == "device.roll_call"
        assert envelope["capability_version"] == 1
        assert envelope["dst"]["id"] == GUARD_DEVICE
        assert envelope["src"] == {
            "type": "companion",
            "id": COMPANION_A,
            "owner_id": OWNER_A,
            "source_device_id": BOX_DEVICE,
        }
        persisted = await stack.store.body_commands.get_command(result.content["command_id"])
        assert persisted is not None
        assert persisted.status == "succeeded"
        assert persisted.result_json == {"played": True}
        assert persisted.envelope_json["_hub_runtime"]["trace_id"] == _caller().trace_id
        assert persisted.envelope_json["_hub_capability_contract"]["version"] == 1


@pytest.mark.asyncio
async def test_contract_version_change_between_tool_assembly_and_invoke_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path) as stack:
        _schemas, old_ports = await _tools(stack)
        await _register(
            stack.http,
            device_id=GUARD_DEVICE,
            manifest={
                "device": {"name": "ATK Guard", "kind": "atk-guard"},
                "capabilities": [
                    _capability(
                        "device.roll_call",
                        "Play local roll-call response v2",
                        version=2,
                    )
                ],
                "guard": True,
                "guard_protocol_versions": [1],
            },
        )

        result = await old_ports[CAPABILITY_TOOL].invoke(
            ToolCall(
                id="stale-v1-call",
                name=CAPABILITY_TOOL,
                arguments={"target_companion": GUARD_COMPANION_NAME},
            ),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="stale-v1-turn"),
        )

        assert result.ok is False
        assert result.error_code == "body_capability_unsupported"
        assert stack.room.sent_envelopes == []


@pytest.mark.asyncio
async def test_invalid_device_result_schema_is_not_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path, guard_behavior="invalid_result") as stack:
        _schemas, ports = await _tools(stack)
        result = await ports[CAPABILITY_TOOL].invoke(
            ToolCall(
                id="invalid-result-call",
                name=CAPABILITY_TOOL,
                arguments={"target_companion": GUARD_COMPANION_NAME},
            ),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="invalid-result-turn"),
        )

        assert result.ok is False
        assert result.error_code == "failed"
        assert "invalid capability result" in (result.error_message or "")
        assert result.content["completed"] is False


@pytest.mark.asyncio
async def test_tampered_signed_registration_body_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path) as stack:
        payload = await _register(
            stack.http,
            device_id="tampered-guard",
            manifest={
                "device": {"name": "ATK Guard", "kind": "atk-guard"},
                "capabilities": [_capability("device.roll_call", "Play roll call")],
                "guard": True,
                "guard_protocol_versions": [1],
            },
            tamper_after_signing=True,
        )
        assert payload["status"] == "rejected"
        assert await stack.store.devices.get_device("tampered-guard") is None


@pytest.mark.asyncio
async def test_private_capability_is_hidden_and_hub_rejects_direct_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path, visibility="bound_companion") as stack:
        _schemas, ports = await _tools(stack)
        assert CAPABILITY_TOOL not in ports

        response = await stack.http.post(
            f"/api/runtime/devices/{GUARD_DEVICE}/commands",
            json=_runtime_request(),
            headers=_service_headers(),
        )
        assert response.status_code == 403
        assert stack.room.sent_envelopes == []


@pytest.mark.asyncio
async def test_cross_owner_target_is_hidden_and_rejected_by_hub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path, guard_owner=OWNER_B) as stack:
        _schemas, ports = await _tools(stack)
        assert CAPABILITY_TOOL not in ports

        response = await stack.http.post(
            f"/api/runtime/devices/{GUARD_DEVICE}/commands",
            json=_runtime_request(),
            headers=_service_headers(),
        )
        assert response.status_code == 403
        assert stack.room.sent_envelopes == []


@pytest.mark.asyncio
async def test_dynamic_declared_op_is_visible_but_undeclared_op_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(
        tmp_path,
        guard_capabilities=[_capability("vendor.ping", "Play a vendor ping")],
    ) as stack:
        _schemas, ports = await _tools(stack)
        vendor_tool = "cap_vendor_ping_v1"
        assert vendor_tool in ports

        undeclared = await stack.http.post(
            f"/api/runtime/devices/{GUARD_DEVICE}/commands",
            json=_runtime_request(),
            headers=_service_headers(),
        )
        declared_result = await ports[vendor_tool].invoke(
            ToolCall(
                id="tool-call-vendor",
                name=vendor_tool,
                arguments={
                    "target_companion": GUARD_COMPANION_NAME,
                },
            ),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="turn-vendor"),
        )
        assert undeclared.status_code == 409
        assert declared_result.ok is True
        assert stack.room.sent_envelopes[-1]["op"] == "vendor.ping"


@pytest.mark.asyncio
async def test_offline_guard_tool_fails_without_sending_or_claiming_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path, guard_behavior="offline") as stack:
        _schemas, ports = await _tools(stack)
        assert CAPABILITY_TOOL not in ports
        assert stack.room.sent_envelopes == []


@pytest.mark.asyncio
async def test_missing_terminal_result_returns_timeout_not_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path, guard_behavior="timeout") as stack:
        _schemas, ports = await _tools(stack)
        result = await ports[CAPABILITY_TOOL].invoke(
            ToolCall(
                id="tool-call-timeout",
                name=CAPABILITY_TOOL,
                arguments={
                    "target_companion": GUARD_COMPANION_NAME,
                },
            ),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="turn-timeout"),
        )
        assert result.ok is False
        assert result.error_code == "timeout"
        assert result.content["status"] == "timeout"
        assert len(stack.room.sent_envelopes) == 1
