from __future__ import annotations

import pytest
from eidolon_sdk.biz.body import (
    KNOWN_BODY_CAPABILITIES,
    BodyCapability,
    BodyCommandResult,
    BodyDevice,
)

from eidolon_agent.domain.body_control.errors import (
    BodyCapabilityUnsupported,
    BodyCommandRejected,
    BodyCompanionNotFound,
    BodyDeviceAmbiguous,
    BodyDeviceOffline,
)
from eidolon_agent.domain.body_control.service import BodyControlService


@pytest.mark.asyncio
async def test_send_command_resolves_named_online_device() -> None:
    commands = _FakeCommands()
    service = BodyControlService(
        device_store=_FakeDevices(
            [
                _device("device-206", "2.06", current=True),
                _device("box-3", "box-3", aliases=("音箱",)),
            ]
        ),
        command_port=commands,
    )

    result = await service.send_command(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id="device-206",
        runtime_session_id="rs-1",
        target="box-3",
        op="sound.play",
        payload={"sound": "ping"},
    )

    assert result.ok
    assert commands.sent == [
        {
            "owner_id": "owner-1",
            "companion_id": "companion-1",
            "device_id": "box-3",
            "source_device_id": "device-206",
            "runtime_session_id": "rs-1",
            "op": "sound.play",
            "capability_version": 1,
            "payload": {"sound": "ping"},
            "qos": "ack",
            "ttl_ms": 30000,
            "priority": "normal",
            "runtime_trace_id": None,
            "runtime_turn_id": None,
            "runtime_tool_call_id": None,
            "idempotency_key": None,
        }
    ]


@pytest.mark.asyncio
async def test_current_device_alias_uses_source_device() -> None:
    commands = _FakeCommands()
    service = BodyControlService(
        device_store=_FakeDevices(
            [
                _device("device-206", "2.06", current=True),
                _device("box-3", "box-3"),
            ]
        ),
        command_port=commands,
    )

    await service.send_command(
        owner_id="owner-1",
        companion_id="companion-1",
        source_device_id="device-206",
        target="这个设备",
        op="sound.play",
        payload={"sound": "ping"},
    )

    assert commands.sent[0]["device_id"] == "device-206"


@pytest.mark.asyncio
async def test_offline_device_is_not_sent() -> None:
    service = BodyControlService(
        device_store=_FakeDevices([_device("box-3", "box-3", status="offline")]),
        command_port=_FakeCommands(),
    )

    with pytest.raises(BodyDeviceOffline):
        await service.send_command(
            owner_id="owner-1",
            companion_id="companion-1",
            source_device_id=None,
            target="box-3",
            op="sound.play",
            payload={"sound": "ping"},
        )


@pytest.mark.asyncio
async def test_unsupported_capability_is_rejected() -> None:
    service = BodyControlService(
        device_store=_FakeDevices([_device("box-3", "box-3", capabilities=("display.update",))]),
        command_port=_FakeCommands(),
    )

    with pytest.raises(BodyCapabilityUnsupported):
        await service.send_command(
            owner_id="owner-1",
            companion_id="companion-1",
            source_device_id=None,
            target="box-3",
            op="sound.play",
            payload={"sound": "ping"},
        )


@pytest.mark.asyncio
async def test_payload_schema_is_validated_before_send() -> None:
    commands = _FakeCommands()
    service = BodyControlService(
        device_store=_FakeDevices([_device("box-3", "box-3")]),
        command_port=commands,
    )

    with pytest.raises(BodyCommandRejected, match="missing required"):
        await service.send_command(
            owner_id="owner-1",
            companion_id="companion-1",
            source_device_id=None,
            target="box-3",
            op="sound.play",
            payload={},
        )

    assert commands.sent == []


@pytest.mark.asyncio
async def test_invalid_qos_is_rejected_before_send() -> None:
    commands = _FakeCommands()
    service = BodyControlService(
        device_store=_FakeDevices([_device("box-3", "box-3")]),
        command_port=commands,
    )

    with pytest.raises(BodyCommandRejected, match="qos"):
        await service.send_command(
            owner_id="owner-1",
            companion_id="companion-1",
            source_device_id=None,
            target="box-3",
            op="sound.play",
            payload={"sound": "ping"},
            qos="later",
        )

    assert commands.sent == []


@pytest.mark.asyncio
async def test_command_status_is_scoped_to_visible_devices() -> None:
    commands = _FakeCommands(status_device_id="other-device")
    service = BodyControlService(
        device_store=_FakeDevices([_device("box-3", "box-3")]),
        command_port=commands,
    )

    with pytest.raises(BodyCommandRejected, match="visible body device"):
        await service.get_command_status(
            owner_id="owner-1",
            companion_id="companion-1",
            command_id="cmd-foreign",
        )


@pytest.mark.asyncio
async def test_ambiguous_target_requires_clarification() -> None:
    service = BodyControlService(
        device_store=_FakeDevices(
            [
                _device("box-3a", "box", aliases=("音箱",)),
                _device("box-3b", "box", aliases=("音箱",)),
            ]
        ),
        command_port=_FakeCommands(),
    )

    with pytest.raises(BodyDeviceAmbiguous):
        await service.send_command(
            owner_id="owner-1",
            companion_id="companion-1",
            source_device_id=None,
            target="音箱",
            op="sound.play",
            payload={"sound": "ping"},
        )


@pytest.mark.asyncio
async def test_companion_capability_resolves_current_provider_device() -> None:
    commands = _FakeCommands()
    service = BodyControlService(
        device_store=_FakeDevices(
            [
                _device(
                    "light-1",
                    "physical-light",
                    companion_id="companion-living-room",
                    companion_name="客厅",
                    capabilities=("sound.play",),
                )
            ]
        ),
        command_port=commands,
    )

    await service.send_companion_capability(
        owner_id="owner-1",
        companion_id="requester-companion",
        source_device_id="source-device",
        target_companion="客厅",
        capability_name="sound.play",
        capability_version=1,
        payload={"sound": "ping"},
    )

    assert commands.sent[0]["device_id"] == "light-1"
    assert commands.sent[0]["op"] == "sound.play"
    assert commands.sent[0]["qos"] == "result"


@pytest.mark.asyncio
async def test_companion_capability_does_not_guess_missing_name() -> None:
    service = BodyControlService(
        device_store=_FakeDevices(
            [
                _device(
                    "light-1",
                    "physical-light",
                    companion_id="companion-living-room",
                    companion_name="客厅",
                )
            ]
        ),
        command_port=_FakeCommands(),
    )

    with pytest.raises(BodyCompanionNotFound):
        await service.send_companion_capability(
            owner_id="owner-1",
            companion_id="requester-companion",
            source_device_id=None,
            target_companion="客",
            capability_name="sound.play",
            capability_version=1,
            payload={"sound": "ping"},
        )


@pytest.mark.asyncio
async def test_companion_capability_rejects_multiple_current_providers() -> None:
    service = BodyControlService(
        device_store=_FakeDevices(
            [
                _device(
                    "light-1",
                    "physical-light-1",
                    companion_id="companion-living-room",
                    companion_name="客厅",
                ),
                _device(
                    "light-2",
                    "physical-light-2",
                    companion_id="companion-living-room",
                    companion_name="客厅",
                ),
            ]
        ),
        command_port=_FakeCommands(),
    )

    with pytest.raises(BodyDeviceAmbiguous):
        await service.send_companion_capability(
            owner_id="owner-1",
            companion_id="requester-companion",
            source_device_id=None,
            target_companion="客厅",
            capability_name="sound.play",
            capability_version=1,
            payload={"sound": "ping"},
        )


def _device(
    device_id: str,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    current: bool = False,
    status: str = "online_control",
    capabilities: tuple[str, ...] = ("sound.play",),
    companion_id: str = "provider-companion",
    companion_name: str = "Provider Companion",
) -> BodyDevice:
    return BodyDevice(
        device_id=device_id,
        name=name,
        aliases=aliases,
        provider_companion_id=companion_id,
        provider_companion_name=companion_name,
        status=status,
        is_current_device=current,
        capabilities=tuple(
            KNOWN_BODY_CAPABILITIES.get(name, BodyCapability(name=name)) for name in capabilities
        ),
    )


class _FakeDevices:
    def __init__(self, devices: list[BodyDevice]) -> None:
        self.devices = devices

    async def list_devices(self, **_kwargs):
        return self.devices


class _FakeCommands:
    def __init__(self, *, status_device_id: str = "box-3") -> None:
        self.sent: list[dict] = []
        self.status_device_id = status_device_id

    async def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return BodyCommandResult(
            command_id="cmd-1",
            device_id=kwargs["device_id"],
            op=kwargs["op"],
            status="sent",
        )

    async def get_command_status(self, *, command_id: str, **_kwargs):
        return BodyCommandResult(
            command_id=command_id,
            device_id=self.status_device_id,
            op="sound.play",
            status="done",
        )
