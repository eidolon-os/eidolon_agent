"""Fakes for the smart-home ports: a directory and a tiny virtual provider."""

from __future__ import annotations

import asyncio
import time

import pytest
from eidolon_sdk.biz.smarthome import (
    ERROR_DEADLINE_EXCEEDED,
    ERROR_DEVICE_OFFLINE,
    ERROR_UNKNOWN_SCENE,
    CommandResult,
    ExecuteRequest,
    ExecuteResult,
    Placement,
    Registry,
    initial_state,
)
from eidolon_sdk.biz.smarthome.samples import apartment

from eidolon_agent.domain.smarthome import DeviceStatus, HomeSnapshot, SmartHomeUnavailable

OWNER = "owner-1"


def home_registry(**placements: str) -> Registry:
    """The SDK sample apartment, with Eidolon devices placed as ``device_ref=area_id``."""
    data = apartment().model_dump()
    data["placements"] = [
        Placement(device_ref=ref, area_id=area).model_dump() for ref, area in placements.items()
    ]
    return Registry.model_validate(data)


class FakeDirectory:
    def __init__(self, registry: Registry, status: dict[str, DeviceStatus] | None = None) -> None:
        self.registries = [registry]
        self.status = status if status is not None else _initial(registry)
        self.unavailable = False
        self.calls = 0

    async def snapshot(self, owner_id: str) -> HomeSnapshot:
        assert owner_id == OWNER
        self.calls += 1
        if self.unavailable:
            raise SmartHomeUnavailable("runtime down")
        registry = self.registries[min(self.calls, len(self.registries)) - 1]
        return HomeSnapshot(registry=registry, status=dict(self.status))


class FakeExecutor:
    """Applies commands to the directory's status like the virtual provider would.

    A scene is expanded here, from the registry as stored now, as the Runtime does.
    """

    def __init__(self, directory: FakeDirectory) -> None:
        self._directory = directory
        self.requests: list[ExecuteRequest] = []
        self.offline: set[str] = set()
        self.unknown: set[str] = set()
        # Devices behind a platform that only answers in words.
        self.delegated: dict[str, str] = {}
        self.delay_s = 0.0
        self.unavailable = False
        self.scenes = {s.scene_id: s for s in directory.registries[-1].scenes}

    async def execute(self, owner_id: str, request: ExecuteRequest) -> ExecuteResult:
        assert owner_id == OWNER
        if self.unavailable:
            raise SmartHomeUnavailable("runtime down")
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if request.deadline_ms < time.time() * 1000:
            return ExecuteResult(request_id=request.request_id, error=ERROR_DEADLINE_EXCEEDED)
        commands = request.commands
        if request.scene_id is not None:
            scene = self.scenes.get(request.scene_id)
            if scene is None:
                return ExecuteResult(request_id=request.request_id, error=ERROR_UNKNOWN_SCENE)
            commands = scene.actions
        results = []
        for command in commands:
            device_id = command.device_id
            if device_id in self.offline:
                results.append(
                    CommandResult(device_id=device_id, status="failed", code=ERROR_DEVICE_OFFLINE)
                )
                continue
            if device_id in self.unknown:
                results.append(CommandResult(device_id=device_id, status="unknown"))
                continue
            if device_id in self.delegated:
                results.append(
                    CommandResult(
                        device_id=device_id, status="delegated",
                        platform_answer=self.delegated[device_id],
                    )
                )
                continue
            current = self._directory.status[device_id]
            state = _apply(dict(current.state), command.trait, command.command, command.params)
            self._directory.status[device_id] = DeviceStatus(online=True, state=state)
            results.append(CommandResult(device_id=device_id, status="succeeded", state=state))
        return ExecuteResult(request_id=request.request_id, results=tuple(results))

    @property
    def commands(self) -> list[tuple[str, str, str, dict]]:
        """Explicit commands submitted (a scene request carries none)."""
        return [
            (c.device_id, c.trait, c.command, dict(c.params))
            for request in self.requests
            for c in request.commands
        ]


def _initial(registry: Registry) -> dict[str, DeviceStatus]:
    return {
        d.device_id: DeviceStatus(online=True, state=initial_state(d.type))
        for d in registry.devices
    }


def _apply(state: dict, trait: str, command: str, params: dict) -> dict:
    key = (trait, command)
    if key == ("on_off", "on"):
        state["on"] = True
    elif key == ("on_off", "off"):
        state["on"] = False
    elif key == ("level", "set"):
        state["level"] = params["value"]
    elif key == ("level", "step"):
        state["level"] = max(0, min(100, state["level"] + params["delta"]))
    elif key == ("thermostat", "set_target"):
        state["target_c"] = params["celsius"]
    elif key == ("thermostat", "step"):
        state["target_c"] = state["target_c"] + params["delta"]
    elif key == ("thermostat", "set_mode"):
        state["mode"] = params["mode"]
    elif key == ("position", "open"):
        state["position"] = 100
    elif key == ("position", "close"):
        state["position"] = 0
    elif key == ("position", "set"):
        state["position"] = params["value"]
    elif key == ("lock", "lock"):
        state["locked"] = True
    elif key == ("lock", "unlock"):
        state["locked"] = False
    elif key == ("operational", "start"):
        state["run_state"] = "running"
    elif key == ("operational", "stop"):
        state["run_state"] = "idle"
    elif key == ("volume", "set"):
        state["volume"] = params["value"]
    elif key == ("volume", "step"):
        state["volume"] = max(0, min(100, state["volume"] + params["delta"]))
    elif key == ("volume", "mute"):
        state["muted"] = params["muted"]
    elif key == ("fan_speed", "set"):
        state["speed"] = params["value"]
    return state


@pytest.fixture
def directory() -> FakeDirectory:
    return FakeDirectory(home_registry(**{"panel-living": "living", "box3-master": "master"}))


@pytest.fixture
def executor(directory: FakeDirectory) -> FakeExecutor:
    return FakeExecutor(directory)
