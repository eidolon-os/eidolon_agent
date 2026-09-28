"""LLM-facing smart-home tools, for the LLM fallback and later companion turns.

Six fixed tools addressed by name / area / device type, after Home Assistant's
built-in intents (never one tool per device). Each call re-reads the directory
and goes through the same planning and execute path as a spoken command, so a
tool can only claim what the Provider confirmed.

Built but not registered: wiring into bootstrap comes with the command session.
"""

from __future__ import annotations

import json
from typing import Any

from eidolon_sdk.biz.interpretation import Action, Slot
from eidolon_sdk.biz.smarthome import DEVICE_TYPES, Device, Scene
from eidolon_sdk.biz.smarthome import Origin as ExecuteOrigin

from eidolon_agent.core.ports.tool import ToolInvocationContext, ToolPort
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema
from eidolon_agent.domain.smarthome.addressing import Address, resolve_address
from eidolon_agent.domain.smarthome.errors import SmartHomeUnavailable
from eidolon_agent.domain.smarthome.messages import (
    NO_SUCH_DEVICE,
    UNAVAILABLE,
    choose_one,
    describe,
    summarize,
)
from eidolon_agent.domain.smarthome.planning import (
    UNKNOWN,
    CommandPlan,
    Execution,
    HomeActuator,
    PlannedTarget,
    identifier,
    plan_devices,
    plan_scene,
    request_id,
)
from eidolon_agent.domain.smarthome.ports import (
    HomeSnapshot,
    SmartHomeDirectoryPort,
    SmartHomeExecutePort,
)

_PERMISSIONS = frozenset({Permission.SYSTEM, Permission.USER_DATA})
_IDEMPOTENCY = "smarthome:${tool_name}:${owner_id}:${trace_id}:${arguments_json}"
_ADDRESS: dict[str, Any] = {
    "name": {
        "type": "string",
        "description": "Device name or alias exactly as the Owner calls it, e.g. 客厅主灯.",
    },
    "area": {
        "type": "string",
        "description": "Room name, e.g. 客厅. Omit to mean the room the speaker is in.",
    },
    "device_type": {
        "type": "string",
        "enum": sorted(DEVICE_TYPES),
        "description": "Device type, used with or instead of a name.",
    },
    "all": {
        "type": "boolean",
        "description": "True to act on every matching device (所有灯). Otherwise several matches are ambiguous.",
    },
}
_TURN: dict[str, tuple[tuple[str, str], ...]] = {
    "on": (("on_off", "on"), ("position", "open"), ("operational", "start")),
    "off": (("on_off", "off"), ("position", "close"), ("operational", "stop")),
}
# home_set field -> (trait, command, parameter)
_SETTINGS: dict[str, tuple[str, str, str]] = {
    "brightness": ("level", "set", "value"),
    "temperature": ("thermostat", "set_target", "celsius"),
    "mode": ("thermostat", "set_mode", "mode"),
    "fan_speed": ("fan_speed", "set", "value"),
    "position": ("position", "set", "value"),
    "volume": ("volume", "set", "value"),
    "muted": ("volume", "mute", "muted"),
}
# home_adjust setting -> (trait, default step)
_STEPS: dict[str, tuple[str, int]] = {
    "brightness": ("level", 10),
    "temperature": ("thermostat", 1),
    "volume": ("volume", 10),
}
_MODES = sorted({mode for spec in DEVICE_TYPES.values() for mode in spec.modes})


def smart_home_tools(
    directory: SmartHomeDirectoryPort,
    executor: SmartHomeExecutePort,
    *,
    deadline_ms: int = 3000,
) -> tuple[ToolPort, ...]:
    actuator = HomeActuator(executor, deadline_ms=deadline_ms)
    timeout_s = deadline_ms / 1000 + 3.0
    return (
        HomeTurnTool(directory, actuator, on=True, timeout_s=timeout_s),
        HomeTurnTool(directory, actuator, on=False, timeout_s=timeout_s),
        HomeSetTool(directory, actuator, timeout_s=timeout_s),
        HomeAdjustTool(directory, actuator, timeout_s=timeout_s),
        HomeGetStateTool(directory, actuator, timeout_s=timeout_s),
        HomeActivateSceneTool(directory, actuator, timeout_s=timeout_s),
    )


class _HomeTool:
    schema: ToolSchema

    def __init__(self, directory: SmartHomeDirectoryPort, actuator: HomeActuator) -> None:
        self._directory = directory
        self._actuator = actuator

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        try:
            home = await self._directory.snapshot(ctx.turn_context.owner_id)
            return await self._call(home, call, ctx)
        except SmartHomeUnavailable:
            return self._error(call, "smarthome_unavailable", UNAVAILABLE)

    async def _call(
        self, home: HomeSnapshot, call: ToolCall, ctx: ToolInvocationContext
    ) -> ToolResult:
        raise NotImplementedError

    def _targets(
        self, home: HomeSnapshot, call: ToolCall, ctx: ToolInvocationContext, *, every: bool = False
    ) -> tuple[Device, ...] | ToolResult:
        args = call.arguments
        address = Address(
            name=_text(args.get("name")),
            area=_text(args.get("area")),
            device_type=_text(args.get("device_type")),
            all=every or args.get("all") is True,
        )
        if address.empty:
            return self._error(call, "invalid_arguments", "name, area or device_type is required")
        device_ref = ctx.turn_context.device_id
        origin_area = home.registry.area_of(device_ref) if device_ref else None
        found = resolve_address(home.registry, address, origin_area=origin_area)
        if found.status == "not_found":
            return self._error(call, "not_found", NO_SUCH_DEVICE)
        if found.status == "ambiguous":
            return self._error(
                call,
                "ambiguous",
                choose_one([d.name for d in found.devices]),
                content={"candidates": [_brief(home, d) for d in found.devices]},
            )
        return found.devices

    async def _actuate(
        self, call: ToolCall, ctx: ToolInvocationContext, plan: CommandPlan
    ) -> ToolResult:
        arguments = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, default=str)
        execution = await self._actuator.execute(
            ctx.turn_context.owner_id,
            plan,
            # One logical action per turn and arguments, whatever call id the LLM used.
            request_id=request_id(
                "llm", ctx.turn_context.trace_id or ctx.turn_id, self.schema.name, arguments
            ),
            origin=ExecuteOrigin(
                kind="voice" if ctx.input_modality == "voice" else "text",
                device_ref=identifier(ctx.turn_context.device_id),
                turn_id=identifier(ctx.turn_id),
            ),
        )
        completion, message = summarize(plan, execution)
        complete = completion == "complete"
        receipt = {
            "kind": "external_action_receipt",
            "completed": complete,
            "completion": completion,
            "message": message,
            "error": execution.error,
            "devices": _receipt_devices(plan, execution),
        }
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=complete,
            content=receipt,
            error_code=None if complete else f"{completion}_completed",
            error_message=None if complete else message,
            metadata={"external_action": True, "completed": complete},
        )

    def _error(self, call: ToolCall, code: str, message: str, *, content: Any = None) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=False,
            content=content,
            error_code=code,
            error_message=message,
            metadata={"external_action": self.schema.side_effect, "completed": False},
        )


def _actuating_schema(
    name: str, description: str, properties: dict, required: list[str], timeout_s: float
) -> ToolSchema:
    return ToolSchema(
        name=name,
        description=(
            f"{description} Do not claim the home changed unless this tool returns "
            "completed=true; relay its message otherwise."
        ),
        json_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        permissions=_PERMISSIONS,
        side_effect=True,
        timeout_s=timeout_s,
        idempotency_key_template=_IDEMPOTENCY,
    )


class HomeTurnTool(_HomeTool):
    """``home_turn_on`` / ``home_turn_off``: power, covers open/close, appliances start/stop."""

    def __init__(
        self,
        directory: SmartHomeDirectoryPort,
        actuator: HomeActuator,
        *,
        on: bool,
        timeout_s: float,
    ) -> None:
        super().__init__(directory, actuator)
        self._op = "on" if on else "off"
        verb = "Turn on / open / start" if on else "Turn off / close / stop"
        self.schema = _actuating_schema(
            f"home_turn_{self._op}",
            f"{verb} smart-home devices, addressed by name, area and/or device_type.",
            dict(_ADDRESS),
            [],
            timeout_s,
        )

    async def _call(self, home, call, ctx) -> ToolResult:
        targets = self._targets(home, call, ctx)
        if isinstance(targets, ToolResult):
            return targets
        return await self._actuate(call, ctx, plan_devices(targets, self._action))

    def _action(self, device: Device) -> Action | None:
        traits = DEVICE_TYPES[device.type].traits
        for trait, command in _TURN[self._op]:
            if trait in traits:
                return Action(trait=trait, command=command)
        return None


class HomeSetTool(_HomeTool):
    """``home_set``: one absolute setting (brightness, temperature, position…)."""

    def __init__(
        self, directory: SmartHomeDirectoryPort, actuator: HomeActuator, *, timeout_s: float
    ) -> None:
        super().__init__(directory, actuator)
        self.schema = _actuating_schema(
            "home_set",
            "Set exactly one value on smart-home devices: brightness 0-100, temperature "
            "in °C, climate mode, fan_speed 0-100, curtain position 0-100, volume 0-100, "
            "or muted.",
            {
                **_ADDRESS,
                "brightness": {"type": "integer"},
                "temperature": {"type": "number"},
                "mode": {"type": "string", "enum": _MODES},
                "fan_speed": {"type": "integer"},
                "position": {"type": "integer"},
                "volume": {"type": "integer"},
                "muted": {"type": "boolean"},
            },
            [],
            timeout_s,
        )

    async def _call(self, home, call, ctx) -> ToolResult:
        chosen = [key for key in _SETTINGS if key in call.arguments]
        if len(chosen) != 1:
            return self._error(call, "invalid_arguments", "give exactly one setting")
        trait, command, param = _SETTINGS[chosen[0]]
        action = Action(
            trait=trait,
            command=command,
            slots=(Slot(name=param, value=call.arguments[chosen[0]]),),
        )
        targets = self._targets(home, call, ctx)
        if isinstance(targets, ToolResult):
            return targets
        return await self._actuate(call, ctx, plan_devices(targets, lambda _d: action))


class HomeAdjustTool(_HomeTool):
    """``home_adjust``: relative change (brighter, warmer, louder)."""

    def __init__(
        self, directory: SmartHomeDirectoryPort, actuator: HomeActuator, *, timeout_s: float
    ) -> None:
        super().__init__(directory, actuator)
        self.schema = _actuating_schema(
            "home_adjust",
            "Raise or lower brightness (percent points), temperature (°C) or volume by an "
            "amount; without an amount use a small default step.",
            {
                **_ADDRESS,
                "setting": {"type": "string", "enum": sorted(_STEPS)},
                "direction": {"type": "string", "enum": ["up", "down"]},
                "amount": {"type": "number"},
            },
            ["setting", "direction"],
            timeout_s,
        )

    async def _call(self, home, call, ctx) -> ToolResult:
        args = call.arguments
        step = _STEPS.get(str(args.get("setting")))
        direction = args.get("direction")
        amount = args.get("amount", step[1] if step else None)
        if step is None or direction not in ("up", "down") or not _number(amount):
            return self._error(call, "invalid_arguments", "setting, direction or amount invalid")
        if isinstance(amount, float) and amount.is_integer():
            amount = int(amount)
        delta = abs(amount) if direction == "up" else -abs(amount)
        action = Action(trait=step[0], command="step", slots=(Slot(name="delta", value=delta),))
        targets = self._targets(home, call, ctx)
        if isinstance(targets, ToolResult):
            return targets
        return await self._actuate(call, ctx, plan_devices(targets, lambda _d: action))


class HomeGetStateTool(_HomeTool):
    """``home_get_state``: read state; without an address, the whole home."""

    def __init__(
        self, directory: SmartHomeDirectoryPort, actuator: HomeActuator, *, timeout_s: float
    ) -> None:
        super().__init__(directory, actuator)
        self.schema = ToolSchema(
            name="home_get_state",
            description=(
                "Read the current state of smart-home devices, addressed by name, area "
                "and/or device_type; with no arguments, list every device and scene."
            ),
            json_schema={
                "type": "object",
                "properties": dict(_ADDRESS),
                "required": [],
                "additionalProperties": False,
            },
            permissions=frozenset({Permission.USER_DATA}),
            timeout_s=timeout_s,
        )

    async def _call(self, home, call, ctx) -> ToolResult:
        scenes: list[str] = []
        if not any(_text(call.arguments.get(k)) for k in ("name", "area", "device_type")):
            devices: tuple[Device, ...] = home.registry.devices
            scenes = [s.name for s in home.registry.scenes]
        else:
            # A read is harmless, so every match is reported rather than asked about.
            targets = self._targets(home, call, ctx, every=True)
            if isinstance(targets, ToolResult):
                return targets
            devices = targets
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={"devices": [_brief(home, d) for d in devices], "scenes": scenes},
        )


class HomeActivateSceneTool(_HomeTool):
    """``home_activate_scene``: run one of the Owner's scenes by name."""

    def __init__(
        self, directory: SmartHomeDirectoryPort, actuator: HomeActuator, *, timeout_s: float
    ) -> None:
        super().__init__(directory, actuator)
        self.schema = _actuating_schema(
            "home_activate_scene",
            "Activate one of the Owner's scenes (e.g. 回家, 观影); each step is reported.",
            {"scene": {"type": "string", "description": "Scene name, e.g. 回家 or 回家模式."}},
            ["scene"],
            timeout_s,
        )

    async def _call(self, home, call, ctx) -> ToolResult:
        scene = _find_scene(home.registry.scenes, _text(call.arguments.get("scene")) or "")
        if scene is None:
            names = [s.name for s in home.registry.scenes]
            return self._error(call, "not_found", "没有这个场景", content={"scenes": names})
        return await self._actuate(call, ctx, plan_scene(home.registry, scene))


def _find_scene(scenes: tuple[Scene, ...], wanted: str) -> Scene | None:
    key = "".join(wanted.split())
    for suffix in ("模式", "场景"):
        key = key.removesuffix(suffix)
    for scene in scenes:
        name = scene.name
        for suffix in ("模式", "场景"):
            name = name.removesuffix(suffix)
        if key and key in (scene.scene_id, name):
            return scene
    return None


def _receipt_devices(plan: CommandPlan, execution: Execution) -> list[dict[str, Any]]:
    if execution.error is not None:
        return []  # refused as a whole: no device was touched
    outcomes = execution.outcomes
    if plan.scene is not None and outcomes:
        targets = [plan.known.get(d, PlannedTarget(d, d, "")) for d in outcomes]
    else:
        targets = list(plan.targets)
    return [
        {
            "device_id": t.device_id,
            "name": t.name,
            "status": "rejected" if t.rejected else outcomes.get(t.device_id, UNKNOWN).status,
            "code": t.rejected or outcomes.get(t.device_id, UNKNOWN).code,
            "state": dict(outcomes.get(t.device_id, UNKNOWN).state or {}),
        }
        for t in targets
    ]


def _brief(home: HomeSnapshot, device: Device) -> dict[str, Any]:
    status = home.status.get(device.device_id)
    area = next((a.name for a in home.registry.areas if a.area_id == device.area_id), None)
    return {
        "device_id": device.device_id,
        "name": device.name,
        "area": area,
        "type": device.type,
        "online": None if status is None else status.online,
        "state": None if status is None else dict(status.state),
        "summary": describe(device, status),
    }


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value != 0
