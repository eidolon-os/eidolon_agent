"""Protocol-level E2E for Box-3 -> cross-Companion ATK Guard roll call.

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

from eidolon_agent.core.ports.tool import ToolInvocationContext  # noqa: E402
from eidolon_agent.core.types.identity import (  # noqa: E402
    CallerContext,
    CallerKind,
    Identity,
)
from eidolon_agent.core.types.tool import ToolCall  # noqa: E402
from eidolon_agent.domain.body_control.adapters import (  # noqa: E402
    EidolonDataBodyDeviceStore,
    HubBodyCommandClient,
)
from eidolon_agent.domain.body_control.service import BodyControlService  # noqa: E402
from eidolon_agent.domain.tools.body_capability_provider import (  # noqa: E402
    BodyCapabilityToolProvider,
)

OWNER_A = "owner-1"
OWNER_B = "owner-2"
COMPANION_A = "companion-a"
COMPANION_B = "companion-guard"
BOX_DEVICE = "box-3"
GUARD_DEVICE = "atk-guard"
ROLL_CALL_TOOL = "body__atk_guard__device_roll_call"
SERVICE_TOKEN = "cross-companion-e2e-token"


class _FakeRoomService:
    """LiveKit management surface plus a minimal Guard protocol simulator."""

    def __init__(self, *, guard_behavior: str) -> None:
        self.guard_behavior = guard_behavior
        self.runtime: LiveKitAdminRuntime | None = None
        self.sent_envelopes: list[dict] = []
        self.tasks: list[asyncio.Task] = []

    async def list_rooms(self, _request):
        rooms = [SimpleNamespace(name="box-control")]
        if self.guard_behavior != "offline":
            rooms.append(SimpleNamespace(name="guard-control"))
        return SimpleNamespace(rooms=rooms)

    async def list_participants(self, request):
        participants = {
            "box-control": [SimpleNamespace(identity=BOX_DEVICE, sid="PA_BOX")],
            "guard-control": [SimpleNamespace(identity=GUARD_DEVICE, sid="PA_GUARD")],
        }
        return SimpleNamespace(participants=participants.get(request.room, []))

    async def send_data(self, request):
        envelope = json.loads(bytes(request.data).decode("utf-8"))
        self.sent_envelopes.append(envelope)
        if envelope.get("dst", {}).get("id") != GUARD_DEVICE:
            return SimpleNamespace()
        if self.guard_behavior == "success":
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
                "op": "device.roll_call",
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
                "op": "device.roll_call",
                "status": "completed",
                "code": "OK",
                "result": {"played": True},
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
        provider: BodyCapabilityToolProvider,
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
    runtime = LiveKitAdminRuntime(config, data_store=store)
    runtime._build_livekit_api = lambda: fake_livekit  # type: ignore[method-assign]
    room.runtime = runtime

    app = FastAPI()
    app.include_router(config_router)
    app.include_router(runtime_commands_router)
    app.state.config = config
    app.state.data_store = store
    app.state.device_manager = manager
    app.state.admin_runtime = runtime

    # BodyDeviceStore only needs the runtime presence overlay from the existing
    # admin device view. Keep this adapter local so the test does not import the
    # whole Hub admin package (and consequently the real mDNS daemon).
    @app.get("/api/admin/devices")
    async def _runtime_devices():
        presence = await runtime.get_presence_snapshot()
        return {
            "devices": [
                {
                    "device_id": item.device_id,
                    "status": item.status,
                    "room_name": item.room_name,
                    "last_seen": item.last_seen_at.isoformat()
                    if item.last_seen_at
                    else None,
                }
                for item in presence
            ]
        }
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
    )

    try:
        statuses = {
            BOX_DEVICE: await _register(
                http,
                device_id=BOX_DEVICE,
                manifest={
                    "device": {"name": "ESP BOX-3", "kind": "esp-box-3"},
                    "capabilities": [{"name": "device.identify"}],
                },
            ),
            GUARD_DEVICE: await _register(
                http,
                device_id=GUARD_DEVICE,
                manifest={
                    "device": {"name": "ATK Guard", "kind": "atk-guard"},
                    "capabilities": guard_capabilities
                    if guard_capabilities is not None
                    else [{"name": "device.roll_call"}],
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
        await store.companions.create(
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
        await store.devices.claim_device(
            GUARD_DEVICE,
            owner_id=guard_owner,
            companion_id=COMPANION_B,
            access_policy_json={"capability_visibility": visibility},
        )
        await manager.load()
        await runtime.run_probe_cycle([BOX_DEVICE, GUARD_DEVICE])

        command_client = HubBodyCommandClient(
            http,
            base_url="http://127.0.0.1",
            service_token=SERVICE_TOKEN,
            timeout_s=0.3,
        )
        body = BodyControlService(
            device_store=EidolonDataBodyDeviceStore(store, runtime_client=command_client),
            command_port=command_client,
        )
        stack = _Stack(
            store=store,
            manager=manager,
            runtime=runtime,
            room=room,
            http=http,
            body=body,
            provider=BodyCapabilityToolProvider(body),
            registration_statuses=statuses,
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
) -> str:
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
        return "rejected"
    response.raise_for_status()
    payload = response.json()
    assert payload["config"]["identity"] == device_id
    return str(payload["status"])


def _signed_headers(*, device_id: str, body: bytes) -> dict[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    public_der = key.public_key().public_bytes(
        Encoding.DER,
        PublicFormat.SubjectPublicKeyInfo,
    )
    nonce = f"nonce-{device_id}"
    timestamp = "0"
    signed = canonical_request(
        method="POST",
        path_query="/api/device/register",
        device_id=device_id,
        nonce=nonce,
        timestamp=timestamp,
        body_hash=body_sha256_hex(body),
    )
    return {
        "X-Device-ID": device_id,
        "X-Device-Nonce": nonce,
        "X-Device-Timestamp": timestamp,
        "X-Device-Signature": _b64url(key.sign(signed, ec.ECDSA(hashes.SHA256()))),
        "X-Device-Public-Key": _b64url(public_der),
        "Content-Type": "application/json",
    }


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
        "op": op,
        "payload": {},
        "qos": "result",
        "ttl_ms": 5000,
    }


def _service_headers() -> dict[str, str]:
    return {"X-Eidolon-Service-Token": SERVICE_TOKEN}


@pytest.mark.asyncio
async def test_box_voice_tool_calls_cross_companion_guard_and_waits_for_real_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path) as stack:
        assert stack.registration_statuses == {
            BOX_DEVICE: "pending_approval",
            GUARD_DEVICE: "pending_approval",
        }
        guard_row = await stack.store.devices.get_device(GUARD_DEVICE)
        assert guard_row is not None
        assert guard_row.name == "ATK Guard"
        assert guard_row.kind == "atk-guard"
        assert [item["name"] for item in guard_row.capabilities_json["ops"]] == [
            "device.roll_call"
        ]

        schemas, ports = await _tools(stack)
        assert ROLL_CALL_TOOL in ports
        schema = next(item for item in schemas if item.name == ROLL_CALL_TOOL)
        assert "ATK Guard" in schema.description
        assert "Guard" in schema.description

        result = await ports[ROLL_CALL_TOOL].invoke(
            ToolCall(id="tool-call-1", name=ROLL_CALL_TOOL, arguments={}),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="turn-guard-in-ma"),
        )

        assert result.ok is True
        assert result.content["status"] == "done"
        assert result.content["result"] == {"played": True}
        assert len(stack.room.sent_envelopes) == 1
        envelope = stack.room.sent_envelopes[0]
        assert envelope["op"] == "device.roll_call"
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


@pytest.mark.asyncio
async def test_tampered_signed_registration_body_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path) as stack:
        status = await _register(
            stack.http,
            device_id="tampered-guard",
            manifest={
                "device": {"name": "ATK Guard", "kind": "atk-guard"},
                "capabilities": [{"name": "device.roll_call"}],
                "guard": True,
                "guard_protocol_versions": [1],
            },
            tamper_after_signing=True,
        )
        assert status == "rejected"
        assert await stack.store.devices.get_device("tampered-guard") is None


@pytest.mark.asyncio
async def test_private_capability_is_hidden_and_hub_rejects_direct_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path, visibility="bound_companion") as stack:
        _schemas, ports = await _tools(stack)
        assert ROLL_CALL_TOOL not in ports

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
        assert ROLL_CALL_TOOL not in ports

        response = await stack.http.post(
            f"/api/runtime/devices/{GUARD_DEVICE}/commands",
            json=_runtime_request(),
            headers=_service_headers(),
        )
        assert response.status_code == 403
        assert stack.room.sent_envelopes == []


@pytest.mark.asyncio
async def test_unknown_or_undeclared_op_never_becomes_tool_or_reaches_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(
        tmp_path,
        guard_capabilities=[{"name": "vendor.unreviewed"}],
    ) as stack:
        _schemas, ports = await _tools(stack)
        assert ROLL_CALL_TOOL not in ports

        undeclared = await stack.http.post(
            f"/api/runtime/devices/{GUARD_DEVICE}/commands",
            json=_runtime_request(),
            headers=_service_headers(),
        )
        unknown = await stack.http.post(
            f"/api/runtime/devices/{GUARD_DEVICE}/commands",
            json=_runtime_request(op="vendor.unreviewed"),
            headers=_service_headers(),
        )
        assert undeclared.status_code == 403
        assert unknown.status_code == 403
        assert stack.room.sent_envelopes == []


@pytest.mark.asyncio
async def test_offline_guard_tool_fails_without_sending_or_claiming_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path, guard_behavior="offline") as stack:
        _schemas, ports = await _tools(stack)
        assert ROLL_CALL_TOOL in ports
        result = await ports[ROLL_CALL_TOOL].invoke(
            ToolCall(id="tool-call-offline", name=ROLL_CALL_TOOL, arguments={}),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="turn-offline"),
        )
        assert result.ok is False
        assert result.error_code == "body_device_offline"
        assert stack.room.sent_envelopes == []


@pytest.mark.asyncio
async def test_missing_terminal_result_returns_timeout_not_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EIDOLON_RUNTIME_SERVICE_TOKEN", SERVICE_TOKEN)
    async with _stack(tmp_path, guard_behavior="timeout") as stack:
        _schemas, ports = await _tools(stack)
        result = await ports[ROLL_CALL_TOOL].invoke(
            ToolCall(id="tool-call-timeout", name=ROLL_CALL_TOOL, arguments={}),
            ctx=ToolInvocationContext(caller=_caller(), turn_id="turn-timeout"),
        )
        assert result.ok is False
        assert result.error_code == "timeout"
        assert result.content["status"] == "timeout"
        assert len(stack.room.sent_envelopes) == 1
