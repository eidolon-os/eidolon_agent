"""Short Chinese result text for the panel card (and for the companion to speak).

A message only claims what a Provider confirmed: ``succeeded`` reads as done,
``unknown`` says no confirmation arrived, ``failed`` says why, and
``delegated`` says the platform took it, in the platform's own words.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from eidolon_sdk.biz.interpretation import Action
from eidolon_sdk.biz.smarthome import (
    DEVICE_TYPES,
    ERROR_DEADLINE_EXCEEDED,
    ERROR_DEVICE_OFFLINE,
    ERROR_INVALID_PARAMS,
    ERROR_OUT_OF_RANGE,
    ERROR_UNKNOWN_DEVICE,
    ERROR_UNKNOWN_SCENE,
    ERROR_UNSUPPORTED_COMMAND,
    TRAIT_COMMANDS,
    Command,
    Device,
    StateValue,
)

from eidolon_agent.domain.smarthome.planning import (
    TOO_MANY_COMMANDS,
    UNKNOWN,
    CommandPlan,
    DeviceOutcome,
    Execution,
    PlannedTarget,
)
from eidolon_agent.domain.smarthome.ports import DeviceStatus

MAX_MESSAGE = 120

NOT_UNDERSTOOD = "没听明白，换个说法试试"
UNRELATED = "这里只处理家里的设备"
UNAVAILABLE = "暂时连不上家居服务"
NO_SUCH_DEVICE = "家里没有这个设备"

# How much of a request took effect: all of it, some of it, or none.
Completion = Literal["complete", "partial", "none"]

MODE_LABELS = {"cool": "制冷", "heat": "制热", "auto": "自动", "fan": "送风", "dry": "除湿"}
_RUN_STATES = {"idle": "待机", "running": "运行中", "paused": "已暂停", "docked": "在充电座上"}


def clip(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def names(items: Sequence[str]) -> str:
    return "、".join(items)


def not_found(mention: str | None) -> str:
    return clip(f"家里没有{mention}") if mention else NO_SUCH_DEVICE


def choose_one(candidate_names: Sequence[str]) -> str:
    return clip(f"{names(candidate_names)}，要哪一个？")


def value_question(name: str, kind: str, action: Action) -> str:
    """Explain the rejected numeric parameter using the same SDK limits."""
    params = TRAIT_COMMANDS.get(action.trait, {}).get(action.command, {})
    for slot in action.slots:
        spec = params.get(slot.name)
        if spec is None or not isinstance(slot.value, int | float):
            continue
        _, low, high = spec
        unit = ""
        if (action.trait, action.command, slot.name) == ("thermostat", "set_target", "celsius"):
            limits = DEVICE_TYPES[kind].target_c
            if limits is not None:
                low, high = limits
                unit = "°C"
        if low is not None and high is not None and not low <= slot.value <= high:
            return clip(f"{name}只能设在 {_num(low)}–{_num(high)}{unit}，要设为多少？")
    return clip(f"{name}不支持这个数值，请换一个数值")


# --- state ------------------------------------------------------------------------


def describe(device: Device, status: DeviceStatus | None) -> str:
    """``客厅空调 开着，制冷 26°C`` / ``客厅空调 离线（上次：关着）``."""
    if status is None:
        return f"{device.name} 状态未知"
    text = state_text(device.type, status.state)
    if not status.online:
        return f"{device.name} 离线（上次：{text}）" if text else f"{device.name} 离线"
    return f"{device.name} {text}" if text else f"{device.name} 在线"


def state_text(kind: str, state: Mapping[str, StateValue]) -> str:
    on = state.get("on")
    if kind == "sensor":
        parts = []
        if state.get("temp_c") is not None:
            parts.append(f"{_num(state['temp_c'])}°C")
        if state.get("humidity") is not None:
            parts.append(f"湿度 {_num(state['humidity'])}%")
        return "，".join(parts) or "暂无读数"
    if kind == "cover":
        position = state.get("position")
        if position is None:
            return ""
        return "关着" if position == 0 else "全开" if position == 100 else f"开了 {position}%"
    if kind == "appliance":
        return _RUN_STATES.get(str(state.get("run_state")), "")
    if kind == "lock":
        locked = state.get("locked")
        return "" if locked is None else "已上锁" if locked else "没锁"
    if on is None:
        return ""
    if not on:
        return "关着"
    detail = ""
    if kind == "light" and state.get("level") is not None:
        detail = f"亮度 {state['level']}%"
    elif kind == "climate":
        mode = MODE_LABELS.get(str(state.get("mode")), "")
        target = f"{_num(state['target_c'])}°C" if state.get("target_c") is not None else ""
        detail = f"{mode} {target}".strip()
        if state.get("current_c") is not None:
            detail = f"{detail}，室温 {_num(state['current_c'])}°C".lstrip("，")
    elif kind == "water_heater" and state.get("target_c") is not None:
        detail = f"{_num(state['target_c'])}°C"
    elif kind == "fan" and state.get("speed") is not None:
        detail = f"风速 {state['speed']}%"
    elif kind == "media" and state.get("volume") is not None:
        detail = f"音量 {state['volume']}" + ("，已静音" if state.get("muted") else "")
    return f"开着，{detail}" if detail else "开着"


# --- execution --------------------------------------------------------------------


def summarize(plan: CommandPlan, execution: Execution) -> tuple[Completion, str]:
    """How much took effect, and the message; partial success is said, not hidden."""
    if execution.error is not None:
        return "none", clip(_refused(plan, execution.error))
    outcomes = execution.outcomes
    if plan.scene is not None:
        # The devices the Runtime reported running, named from the registry.
        reported = tuple(plan.known.get(d, PlannedTarget(d, d, "")) for d in outcomes)
        targets = reported or plan.targets
    else:
        targets = plan.targets
    done: list[PlannedTarget] = []
    delegated: list[PlannedTarget] = []
    unknown: list[PlannedTarget] = []
    problems: list[str] = []
    for target in targets:
        if target.rejected is not None:
            problems.append(failure(target.name, target.rejected, target.kind))
            continue
        outcome = outcomes.get(target.device_id, UNKNOWN)
        if outcome.status == "succeeded":
            done.append(target)
        elif outcome.status == "delegated":
            delegated.append(target)
        elif outcome.status == "failed":
            problems.append(failure(target.name, outcome.code, target.kind))
        else:
            unknown.append(target)
    handled = done or delegated
    completion: Completion = (
        "none" if not handled else "complete" if not unknown and not problems else "partial"
    )
    if plan.scene is not None:
        return completion, clip(_scene_message(plan, done + delegated, unknown, problems))
    parts = [_done_message(done, outcomes)] if done else []
    if delegated:
        parts.append(_delegated_message(delegated, outcomes))
    parts.extend(dict.fromkeys(problems))
    if unknown:
        parts.append(f"没收到确认，{names([t.name for t in unknown])}可能没有执行")
    return completion, clip("；".join(parts) or NOT_UNDERSTOOD)


def _delegated_message(delegated: list[PlannedTarget], outcomes: Mapping[str, DeviceOutcome]) -> str:
    """The platform's words, marked as the platform's: no device state is claimed."""
    answers = [outcomes[t.device_id].answer for t in delegated if outcomes[t.device_id].answer]
    subject = names([t.name for t in delegated])
    if len(set(answers)) == 1:
        return f"{subject} 已交给平台，平台回复：{answers[0]}"
    return f"{subject} 已交给平台处理"


def _refused(plan: CommandPlan, code: str) -> str:
    """The Runtime refused the whole request before running any of it."""
    subject = (
        _scene_label(plan.scene.name)
        if plan.scene is not None
        else names([t.name for t in plan.targets if t.rejected is None])
    )
    if code == ERROR_UNKNOWN_SCENE:
        return f"{subject} 已不存在，没有执行"
    return failure(subject, code)


def failure(name: str, code: str | None, kind: str = "") -> str:
    if code == ERROR_DEVICE_OFFLINE:
        return f"{name} 离线，没有执行"
    if code == ERROR_OUT_OF_RANGE:
        spec = DEVICE_TYPES.get(kind)
        if spec is not None and spec.target_c is not None:
            low, high = spec.target_c
            return f"{name} 只能设在 {_num(low)}–{_num(high)}°C"
        return f"{name} 不支持这个数值"
    if code in (ERROR_UNSUPPORTED_COMMAND, ERROR_INVALID_PARAMS):
        return f"{name} 不支持这个操作"
    if code == ERROR_UNKNOWN_DEVICE:
        return f"{name} 已不在家居列表里"
    if code == ERROR_DEADLINE_EXCEEDED:
        return f"{name} 超时，没有执行"
    if code == TOO_MANY_COMMANDS:
        return "一次要控制的设备太多了"
    return f"{name} 没有执行"


def _scene_message(
    plan: CommandPlan, done: list[PlannedTarget], unknown: list[PlannedTarget], problems: list[str]
) -> str:
    assert plan.scene is not None
    label = _scene_label(plan.scene.name)
    if done and not unknown and not problems:
        return f"已执行{label}"
    if not done and not problems:
        return f"没收到确认，{label}可能没有执行"
    parts = [f"{label}：完成 {len(done)} 项"] if done else []
    parts.extend(dict.fromkeys(problems))
    if unknown:
        parts.append(f"{names([t.name for t in unknown])} 没收到确认")
    return "，".join(parts) if done else f"{label}：" + "，".join(parts)


def _scene_label(name: str) -> str:
    return name if name.endswith(("模式", "场景")) else f"{name}模式"


def _done_message(done: list[PlannedTarget], outcomes: Mapping[str, DeviceOutcome]) -> str:
    commands = [t.commands[-1] for t in done]
    first = commands[0]
    same = all(
        (c.trait, c.command, c.params) == (first.trait, first.command, first.params)
        for c in commands
    )
    if same and first.command != "step":
        return done_phrase(names([t.name for t in done]), first, None)
    return "；".join(done_phrase(t.name, t.commands[-1], outcomes[t.device_id].state) for t in done)


def done_phrase(subject: str, command: Command, state: Mapping[str, StateValue] | None) -> str:
    key = (command.trait, command.command)
    params, state = command.params, state or {}
    if key in (("on_off", "on"), ("position", "open")):
        return f"已打开{subject}"
    if key in (("on_off", "off"), ("position", "close")):
        return f"已关闭{subject}"
    if key == ("on_off", "toggle"):
        return f"已切换{subject}"
    if key in (("level", "set"), ("position", "set")):
        return f"{subject} 已调到 {params['value']}%"
    if key == ("level", "step"):
        if state.get("level") is not None:
            return f"{subject} 已调到 {state['level']}%"
        return f"已调{'亮' if _positive(params) else '暗'}{subject}"
    if key == ("thermostat", "set_target"):
        return f"{subject} 已设为 {_num(params['celsius'])}°C"
    if key == ("thermostat", "step"):
        if state.get("target_c") is not None:
            return f"{subject} 已调到 {_num(state['target_c'])}°C"
        return f"已调{'高' if _positive(params) else '低'}{subject}温度"
    if key == ("thermostat", "set_mode"):
        return f"{subject} 已切换到{MODE_LABELS.get(str(params['mode']), params['mode'])}"
    if key == ("fan_speed", "step"):
        if state.get("speed") is not None:
            return f"{subject} 风速已调到 {state['speed']}%"
        return f"已调{'大' if _positive(params) else '小'}{subject}风速"
    if key == ("fan_speed", "set"):
        return f"{subject} 风速已调到 {params['value']}%"
    if key == ("volume", "set"):
        return f"{subject} 音量已调到 {params['value']}"
    if key == ("volume", "step"):
        if state.get("volume") is not None:
            return f"{subject} 音量已调到 {state['volume']}"
        return f"已调{'大' if _positive(params) else '小'}{subject}音量"
    if key == ("volume", "mute"):
        return f"{subject} 已静音" if params.get("muted") else f"{subject} 已取消静音"
    return {
        ("lock", "lock"): f"{subject} 已上锁",
        ("lock", "unlock"): f"{subject} 已开锁",
        ("operational", "start"): f"{subject} 已启动",
        ("operational", "pause"): f"{subject} 已暂停",
        ("operational", "stop"): f"{subject} 已停止",
        ("operational", "dock"): f"{subject} 正在回充",
        ("position", "stop"): f"{subject} 已停止",
    }.get(key, f"{subject} 已执行")


def _positive(params: Mapping[str, object]) -> bool:
    delta = params.get("delta")
    return isinstance(delta, int | float) and delta > 0


def _num(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def action_phrase(action: Action) -> str:
    """Describe a pending structured action without claiming it has executed."""
    key = (action.trait, action.command)
    params = {s.name: s.value for s in action.slots}
    simple = {
        ("on_off", "on"): "打开", ("on_off", "off"): "关闭",
        ("position", "open"): "打开", ("position", "close"): "关上",
        ("operational", "start"): "启动", ("operational", "stop"): "停止",
        ("operational", "pause"): "暂停",
    }
    if key in simple:
        return simple[key]
    if action.command == "step" and isinstance(params.get("delta"), int | float):
        positive = params['delta'] > 0
        return {
            "level": "调亮" if positive else "调暗",
            "thermostat": "调高温度" if positive else "调低温度",
            "volume": "调大音量" if positive else "调小音量",
        }.get(action.trait, "")
    if key == ("thermostat", "set_target") and 'celsius' in params:
        return f"设为{_num(params['celsius'])}°C"
    if key == ("thermostat", "set_mode") and params.get('mode') in MODE_LABELS:
        return f"切换到{MODE_LABELS[params['mode']]}模式"
    if action.command == "set" and 'value' in params:
        prefix = {"level": "亮度调到", "position": "开到", "fan_speed": "风速调到", "volume": "音量调到"}.get(action.trait)
        if prefix:
            suffix = "" if action.trait == "volume" else "%"
            return f"{prefix}{_num(params['value'])}{suffix}"
    return ""
