"""Live-model replay gate for dynamic cross-Companion capability selection.

This intentionally tests only the probabilistic model boundary.  Registration,
blackboard, Hub authorization, command delivery and receipts remain covered by
the deterministic cross-repository protocol E2E suite.

Example:
    uv run python scripts/bench_runtime_capability_selection.py --repeats 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from eidolon_sdk.biz.body import BodyCapability, BodyCommandResult, BodyDevice

from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config import load_settings
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools.body_capability_provider import RuntimeCapabilityToolProvider


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    user: str
    tool_name: str | None
    target_companion: str | None = None
    expected_arguments: dict[str, Any] | None = None


SCENARIOS = (
    Scenario("roll-call-explicit", "点名 ATK Guard。", "cap_device_roll_call_v1", "ATK Guard"),
    Scenario(
        "roll-call-paraphrase",
        "Guard 在吗？让它回应一下。",
        "cap_device_roll_call_v1",
        "ATK Guard",
    ),
    Scenario(
        "roll-call-stt-shape",
        "A T K杠的在吗？让他回应一下。",
        "cap_device_roll_call_v1",
        "ATK Guard",
    ),
    Scenario(
        "same-contract-other-companion",
        "请让门厅伙伴回应一下。",
        "cap_device_roll_call_v1",
        "门厅伙伴",
    ),
    Scenario(
        "typed-capability-on",
        "把客厅的灯打开。",
        "cap_lighting_set_state_v1",
        "客厅伙伴",
        {"enabled": True},
    ),
    Scenario(
        "typed-capability-off",
        "关闭客厅的灯。",
        "cap_lighting_set_state_v1",
        "客厅伙伴",
        {"enabled": False},
    ),
    Scenario("missing-target-clarifies", "让它回应一下。", None),
    Scenario("unavailable-target-does-not-roleplay", "让卧室伙伴把灯打开。", None),
    Scenario("non-action-does-not-call", "ATK Guard 这个名字是什么意思？", None),
)


class _RuntimeBody:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def list_devices(self, **_kwargs) -> list[BodyDevice]:
        roll_call = BodyCapability(
            name="device.roll_call",
            version=1,
            description="Play this Companion's local audible roll-call response.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            result_schema={
                "type": "object",
                "properties": {"played": {"type": "boolean"}},
                "required": ["played"],
                "additionalProperties": False,
            },
        )
        lighting = BodyCapability(
            name="lighting.set_state",
            version=1,
            description="Turn this Companion's connected light on or off.",
            input_schema={
                "type": "object",
                "properties": {"enabled": {"type": "boolean"}},
                "required": ["enabled"],
                "additionalProperties": False,
            },
            result_schema={
                "type": "object",
                "properties": {"applied": {"type": "boolean"}},
                "required": ["applied"],
                "additionalProperties": False,
            },
        )
        return [
            _device("atk-guard", "guard", "ATK Guard", (roll_call,)),
            _device("hall-guard", "hall", "门厅伙伴", (roll_call,)),
            _device("living-light", "living", "客厅伙伴", (lighting,)),
        ]

    async def send_companion_capability(self, **kwargs) -> BodyCommandResult:
        self.sent.append(kwargs)
        result = (
            {"played": True}
            if kwargs["capability_name"] == "device.roll_call"
            else {"applied": True}
        )
        return BodyCommandResult(
            command_id=f"bench-{len(self.sent)}",
            device_id=f"resolved-{kwargs['target_companion']}",
            op=kwargs["capability_name"],
            capability_version=kwargs["capability_version"],
            status="done",
            result=result,
        )


def _device(
    device_id: str,
    companion_id: str,
    companion_name: str,
    capabilities: tuple[BodyCapability, ...],
) -> BodyDevice:
    return BodyDevice(
        device_id=device_id,
        provider_companion_id=companion_id,
        provider_companion_name=companion_name,
        status="online_control",
        capabilities=capabilities,
    )


def _caller(trace_id: str) -> CallerContext:
    return CallerContext(
        identity=Identity(
            owner_id="benchmark-owner",
            companion_id="box-companion",
            device_id="box-3",
            memory_realm_id="benchmark-memory",
            genome_id="benchmark-genome",
        ),
        caller_kind=CallerKind.LIVEKIT_VOICE,
        trace_id=trace_id,
        request_id=trace_id,
        runtime_caller_id="benchmark-caller",
        runtime_session_id="benchmark-session",
        actor_kind="device",
        actor_id="box-3",
        display_name="Benchmark Owner",
        transport="livekit",
    )


def _message(role: MessageRole, content: str, *, tool_call_id: str | None = None) -> ChatMessage:
    return ChatMessage(
        id=uuid4().hex,
        role=role,
        content=content,
        tool_call_id=tool_call_id,
        created_at=datetime.now(UTC),
    )


async def _model_response(llm, messages, schemas, *, request_id: str):
    calls: list[ToolCall] = []
    text_parts: list[str] = []
    started = time.monotonic()
    async for delta in llm.stream(
        messages,
        tools=schemas,
        temperature=0,
        request_id=request_id,
    ):
        if delta.tool_call is not None:
            calls.append(delta.tool_call)
        if delta.text_delta:
            text_parts.append(delta.text_delta)
    return calls, "".join(text_parts), int((time.monotonic() - started) * 1000)


async def _run_scenario(llm, schemas, ports, scenario: Scenario, repeat: int) -> dict[str, Any]:
    trace_id = f"cap-select-{scenario.scenario_id}-{repeat}-{uuid4().hex[:8]}"
    system = _message(
        MessageRole.SYSTEM,
        (
            "你是 Eidolon realtime companion。工具列表是当前在线能力的唯一事实。"
            "用户明确要求在线 Companion 执行动作时，调用匹配工具；不要代替设备声称动作完成。"
            "目标缺失或目标不在工具枚举中时，简短澄清，不要猜测。"
        ),
    )
    messages = [system, _message(MessageRole.USER, scenario.user)]
    calls, initial_text, first_ms = await _model_response(
        llm, messages, schemas, request_id=trace_id
    )
    errors: list[str] = []
    follow_up = ""
    if scenario.tool_name is None:
        if calls:
            errors.append(f"unexpected tool calls: {[call.name for call in calls]}")
        if not initial_text.strip():
            errors.append("expected a clarification/text response")
    else:
        if len(calls) != 1:
            errors.append(f"expected one tool call, got {[call.name for call in calls]}")
        else:
            call = calls[0]
            if call.name != scenario.tool_name:
                errors.append(f"expected {scenario.tool_name}, got {call.name}")
            if call.arguments.get("target_companion") != scenario.target_companion:
                errors.append(
                    f"expected target {scenario.target_companion!r}, "
                    f"got {call.arguments.get('target_companion')!r}"
                )
            for key, value in (scenario.expected_arguments or {}).items():
                if call.arguments.get(key) != value:
                    errors.append(
                        f"expected argument {key}={value!r}, got {call.arguments.get(key)!r}"
                    )
            if not errors and call.name in ports:
                result = await ports[call.name].invoke(
                    call,
                    ctx=ToolInvocationContext(caller=_caller(trace_id), turn_id=trace_id),
                )
                if not result.ok:
                    errors.append(f"tool execution failed: {result.error_code}")
                else:
                    follow_messages = [
                        *messages,
                        ChatMessage(
                            id=uuid4().hex,
                            role=MessageRole.ASSISTANT,
                            content="",
                            tool_calls=(call,),
                            created_at=datetime.now(UTC),
                        ),
                        ChatMessage(
                            id=uuid4().hex,
                            role=MessageRole.TOOL,
                            content=json.dumps(result.content, ensure_ascii=False, sort_keys=True),
                            tool_call_id=call.id,
                            tool_name=call.name,
                            created_at=datetime.now(UTC),
                        ),
                    ]
                    follow_calls, follow_up, _follow_ms = await _model_response(
                        llm,
                        follow_messages,
                        schemas,
                        request_id=f"{trace_id}-receipt",
                    )
                    if follow_calls:
                        errors.append("model called another tool after a completed receipt")
                    if not follow_up.strip():
                        errors.append("model produced no receipt acknowledgement")
    return {
        "scenario_id": scenario.scenario_id,
        "repeat": repeat,
        "passed": not errors,
        "user": scenario.user,
        "tool_calls": [{"name": call.name, "arguments": call.arguments} for call in calls],
        "initial_text": initial_text,
        "follow_up": follow_up,
        "first_response_ms": first_ms,
        "errors": errors,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")

    settings = load_settings()
    llm = _build_llm_router(settings)
    model = llm.model_id
    if model == "fake":
        raise RuntimeError("live capability selection gate requires a configured real LLM")
    body = _RuntimeBody()
    schemas, ports = await RuntimeCapabilityToolProvider(body).assemble(
        _caller("capability-selection-assembly")
    )
    rows = []
    try:
        for repeat in range(1, args.repeats + 1):
            for scenario in SCENARIOS:
                rows.append(await _run_scenario(llm, schemas, ports, scenario, repeat))
    finally:
        await llm.close()
    report = {
        "schema_version": "eidolon.runtime_capability_selection.v1",
        "model": model,
        "repeats": args.repeats,
        "passed": all(row["passed"] for row in rows),
        "passed_count": sum(1 for row in rows if row["passed"]),
        "total_count": len(rows),
        "rows": rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
