"""From an intent to validated SDK commands, and the one execute path.

Voice commands (an interpretation ``Action`` on resolved targets) and the LLM
tools (a verb chosen per device) both end here: every explicit command is validated
with ``biz.smarthome.validate_command`` against the *current* registry, and
executed through :class:`HomeActuator` with an idempotency key and a deadline.
A scene is sent as its ``scene_id``; the Runtime expands it, in one place.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from eidolon_sdk.biz.interpretation import Action
from eidolon_sdk.biz.smarthome import (
    ERROR_UNKNOWN_DEVICE,
    ERROR_UNSUPPORTED_COMMAND,
    MAX_COMMANDS,
    SCENE_COMMAND,
    SCENE_TRAIT,
    TRAIT_COMMANDS,
    Command,
    CommandResult,
    Device,
    ExecuteRequest,
    Origin,
    Registry,
    Scene,
    SmartHomeError,
    StateValue,
    validate_command,
    validate_execute_result,
)
from pydantic import ValidationError

from eidolon_agent.domain.smarthome.ports import SmartHomeExecutePort

_log = logging.getLogger(__name__)

# Rejection code for a request larger than one ExecuteRequest may carry.
TOO_MANY_COMMANDS = "TOO_MANY_COMMANDS"

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class PlannedTarget:
    """One device a request touches; ``rejected`` holds the code if refused up front."""

    device_id: str
    name: str
    kind: str
    commands: tuple[Command, ...] = ()
    rejected: str | None = None


@dataclass(frozen=True, slots=True)
class CommandPlan:
    """Explicit ``commands`` for ``targets``, or one ``scene`` the Runtime expands.

    For a scene, ``targets`` are the devices its stored actions name now (what a
    lost reply leaves unconfirmed) and ``known`` describes whichever devices the
    Runtime reports, since it expands the scene as stored at execution time.
    """

    targets: tuple[PlannedTarget, ...]
    commands: tuple[Command, ...] = ()  # execution order
    scene: Scene | None = None
    known: Mapping[str, PlannedTarget] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeviceOutcome:
    """``delegated``: a platform took the instruction in its own words and
    reported no device state; ``answer`` is what it said. It is neither
    success nor failure and is never read as the device having changed."""

    status: Literal["succeeded", "failed", "unknown", "delegated"]
    code: str | None = None
    state: Mapping[str, StateValue] | None = None
    answer: str | None = None


UNKNOWN = DeviceOutcome("unknown")


@dataclass(frozen=True, slots=True)
class Execution:
    """Per-device outcomes, or ``error``: the request was refused before anything ran."""

    outcomes: Mapping[str, DeviceOutcome] = field(default_factory=dict)
    error: str | None = None


def find_target(registry: Registry, ref: str) -> Device | Scene | None:
    return registry.device(ref) or registry.scene(ref)


def device_command(device: Device, action: Action) -> Command:
    """The SDK command for ``action`` on ``device``; raises SmartHomeError if refused."""
    params_spec = TRAIT_COMMANDS.get(action.trait, {}).get(action.command)
    if params_spec is None:
        raise SmartHomeError(ERROR_UNSUPPORTED_COMMAND, f"{action.trait}.{action.command}")
    try:
        command = Command(
            device_id=device.device_id,
            trait=action.trait,
            command=action.command,
            params={slot.name: slot.value for slot in action.slots if slot.name in params_spec},
        )
    except ValidationError as exc:
        raise SmartHomeError(ERROR_UNSUPPORTED_COMMAND, str(exc)) from exc
    validate_command(device.type, command)
    return command


def plan_action(registry: Registry, refs: Sequence[str], action: Action) -> CommandPlan:
    """One interpretation action on resolved targets, or the activation of one scene."""
    if action.trait == SCENE_TRAIT:
        scene = registry.scene(refs[0]) if len(refs) == 1 else None
        if action.command != SCENE_COMMAND or scene is None:
            raise SmartHomeError(ERROR_UNSUPPORTED_COMMAND, "scene activation needs one scene")
        return plan_scene(registry, scene)
    devices = [registry.device(ref) for ref in refs]
    if any(device is None for device in devices):
        raise SmartHomeError(ERROR_UNKNOWN_DEVICE, "target is not a device")
    return plan_devices([d for d in devices if d is not None], lambda _device: action)


def plan_devices(
    devices: Sequence[Device], choose: Callable[[Device], Action | None]
) -> CommandPlan:
    """``choose`` gives each device its action (None: this type cannot do it)."""
    targets: list[PlannedTarget] = []
    for device in devices:
        action = choose(device)
        if action is None:
            targets.append(
                PlannedTarget(
                    device.device_id, device.name, device.type, rejected=ERROR_UNSUPPORTED_COMMAND
                )
            )
            continue
        try:
            command = device_command(device, action)
        except SmartHomeError as exc:
            targets.append(
                PlannedTarget(device.device_id, device.name, device.type, rejected=exc.code)
            )
            continue
        targets.append(PlannedTarget(device.device_id, device.name, device.type, (command,)))
    return _bounded(CommandPlan(tuple(targets), tuple(c for t in targets for c in t.commands)))


def plan_scene(registry: Registry, scene: Scene) -> CommandPlan:
    """Activate ``scene`` by id; the Runtime expands and validates its actions."""
    known = {d.device_id: PlannedTarget(d.device_id, d.name, d.type) for d in registry.devices}
    stored = dict.fromkeys(a.device_id for a in scene.actions if a.device_id in known)
    return CommandPlan(tuple(known[d] for d in stored), scene=scene, known=known)


def _bounded(plan: CommandPlan) -> CommandPlan:
    if len(plan.commands) <= MAX_COMMANDS:
        return plan
    targets = tuple(
        PlannedTarget(t.device_id, t.name, t.kind, rejected=t.rejected or TOO_MANY_COMMANDS)
        for t in plan.targets
    )
    return CommandPlan(targets)


def identifier(value: str | None) -> str | None:
    """``value`` if the SDK accepts it as an Identifier, else None (optional facts)."""
    return value if value is not None and _REQUEST_ID.match(value) else None


def request_id(prefix: str, *parts: str) -> str:
    """A stable Runtime idempotency key; hashed when the raw form would not fit."""
    raw = ":".join((prefix, *parts))
    if _REQUEST_ID.match(raw):
        return raw
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:40]
    return f"{prefix}:{digest}"


class HomeActuator:
    """Submits a plan and reads back what each device reported."""

    def __init__(
        self,
        executor: SmartHomeExecutePort,
        *,
        deadline_ms: int = 3000,
        grace_s: float = 0.5,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._executor = executor
        self._deadline_ms = deadline_ms
        self._grace_s = grace_s
        self._wall_clock = wall_clock

    async def execute(
        self, owner_id: str, plan: CommandPlan, *, request_id: str, origin: Origin
    ) -> Execution:
        """Raises SmartHomeUnavailable (nothing was executed)."""
        if plan.scene is None and not plan.commands:
            return Execution()
        expected = [t.device_id for t in plan.targets if t.rejected is None]
        request = ExecuteRequest(
            request_id=request_id,
            commands=plan.commands,
            scene_id=plan.scene.scene_id if plan.scene is not None else None,
            origin=origin,
            # Absolute: past it the Runtime runs nothing, even if delivered late.
            deadline_ms=int(self._wall_clock() * 1000) + self._deadline_ms,
        )
        try:
            async with asyncio.timeout(self._deadline_ms / 1000 + self._grace_s):
                result = await self._executor.execute(owner_id, request)
        except TimeoutError:
            _log.warning("smarthome execute %s: no reply by the deadline", request_id)
            return Execution(dict.fromkeys(expected, UNKNOWN))
        if result.request_id != request_id:
            _log.warning("smarthome execute %s answered as %s", request_id, result.request_id)
            return Execution(dict.fromkeys(expected, UNKNOWN))
        if result.error is not None:
            return Execution(error=result.error)
        reported: dict[str, list[CommandResult]] = {}
        for item in result.results:
            reported.setdefault(item.device_id, []).append(item)
        if plan.scene is not None:
            # One result per action the Runtime ran, in the scene's stored order.
            return Execution({device_id: _outcome(items) for device_id, items in reported.items()})
        try:
            validate_execute_result(plan.commands, result)
        except ValueError as exc:
            _log.warning("smarthome execute %s: results do not line up: %s", request_id, exc)
            return Execution(dict.fromkeys(expected, UNKNOWN))
        return Execution(
            {device_id: _outcome(reported.get(device_id, [])) for device_id in expected}
        )


def _outcome(results: list[CommandResult]) -> DeviceOutcome:
    """Failed if any step failed; unknown if any was; delegated if any was;
    succeeded only if every reported step did."""
    failed = next((r for r in results if r.status == "failed"), None)
    if failed is not None:
        return DeviceOutcome("failed", failed.code, results[-1].state)
    if not results or any(r.status == "unknown" for r in results):
        return UNKNOWN
    delegated = [r for r in results if r.status == "delegated"]
    if delegated:
        return DeviceOutcome("delegated", None, None, delegated[-1].platform_answer)
    return DeviceOutcome("succeeded", None, results[-1].state)
