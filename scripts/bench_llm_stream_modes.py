"""Probe the configured LLM stream without companion/runtime dependencies.

This benchmark verifies that the real provider honors the configured thinking
policy while still producing content and tool calls. It never records
reasoning text; only activity counts and timing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config import load_settings
from eidolon_agent.core.types.llm import LLMActivityKind
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolCall, ToolSchema


def _message(
    role: MessageRole,
    content: str,
    *,
    tool_calls: tuple[ToolCall, ...] = (),
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> ChatMessage:
    return ChatMessage(
        id=uuid.uuid4().hex,
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc),
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
    )


async def _collect(router, messages, *, tools=None, request_id: str) -> dict:
    started = time.monotonic()
    first_activity_ms = None
    first_content_ms = None
    first_tool_ms = None
    content_chunks: list[str] = []
    tool_calls: list[ToolCall] = []
    reasoning_activities = 0
    tool_activities = 0
    finish = None

    async for delta in router.stream(
        messages,
        tools=tools,
        temperature=0.0,
        max_tokens=96,
        request_id=request_id,
    ):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if delta.activity is not None and first_activity_ms is None:
            first_activity_ms = elapsed_ms
        if delta.activity is LLMActivityKind.REASONING:
            reasoning_activities += 1
        elif delta.activity is LLMActivityKind.TOOL_CALL:
            tool_activities += 1
        if delta.text_delta is not None:
            if first_content_ms is None and delta.text_delta.strip():
                first_content_ms = elapsed_ms
            content_chunks.append(delta.text_delta)
        if delta.tool_call is not None:
            if first_tool_ms is None:
                first_tool_ms = elapsed_ms
            tool_calls.append(delta.tool_call)
        if delta.finish is not None:
            finish = delta.finish.value

    return {
        "request_id": request_id,
        "total_ms": int((time.monotonic() - started) * 1000),
        "first_activity_ms": first_activity_ms,
        "first_content_ms": first_content_ms,
        "first_tool_ms": first_tool_ms,
        "content_chunk_count": len(content_chunks),
        "content_chars": len("".join(content_chunks)),
        "reasoning_activity_count": reasoning_activities,
        "tool_activity_count": tool_activities,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments}
            for call in tool_calls
        ],
        "finish": finish,
        "_tool_call_objects": tool_calls,
    }


async def _run(repeat: int) -> dict:
    settings = load_settings()
    router = _build_llm_router(settings)
    configured_model = next(
        model for model in settings.llm.models if model.name == settings.llm.default_model
    )
    cases: list[dict] = []
    try:
        for index in range(repeat):
            result = await _collect(
                router,
                [_message(MessageRole.USER, "请只回答：八六九。不要解释。")],
                request_id=f"no-thinking-content-{index + 1}-{uuid.uuid4().hex[:8]}",
            )
            result["case"] = "repeated_short_content"
            cases.append(result)

        history = [
            _message(MessageRole.USER, "本轮验证码是 325。"),
            _message(MessageRole.ASSISTANT, "收到。"),
            _message(MessageRole.USER, "只回答刚才的验证码，不要解释。"),
        ]
        result = await _collect(
            router,
            history,
            request_id=f"no-thinking-history-{uuid.uuid4().hex[:8]}",
        )
        result["case"] = "history_content"
        cases.append(result)

        tool = ToolSchema(
            name="get_current_time",
            description="Return the current local time for an IANA timezone.",
            json_schema={
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
                "required": ["timezone"],
                "additionalProperties": False,
            },
        )
        tool_prompt = [
            _message(
                MessageRole.USER,
                "必须调用 get_current_time 查询上海时间；不要自行猜测时间。",
            )
        ]
        tool_result = await _collect(
            router,
            tool_prompt,
            tools=[tool],
            request_id=f"no-thinking-tool-{uuid.uuid4().hex[:8]}",
        )
        tool_result["case"] = "tool_call"
        cases.append(tool_result)

        if tool_result["_tool_call_objects"]:
            call = tool_result["_tool_call_objects"][0]
            followup = [
                *tool_prompt,
                _message(MessageRole.ASSISTANT, "", tool_calls=(call,)),
                _message(
                    MessageRole.TOOL,
                    '{"timezone":"Asia/Shanghai","time":"13:25"}',
                    tool_call_id=call.id,
                    tool_name=call.name,
                ),
            ]
            final_result = await _collect(
                router,
                followup,
                tools=[tool],
                request_id=f"no-thinking-tool-result-{uuid.uuid4().hex[:8]}",
            )
            final_result["case"] = "content_after_tool_result"
            cases.append(final_result)
    finally:
        await router.close()

    for case in cases:
        case.pop("_tool_call_objects", None)

    content_cases = [
        case
        for case in cases
        if case["case"]
        in {"repeated_short_content", "history_content", "content_after_tool_result"}
    ]
    tool_cases = [case for case in cases if case["case"] == "tool_call"]
    checks = {
        "configured_thinking_disabled": configured_model.thinking == "disabled",
        "no_reasoning_activity": all(
            case["reasoning_activity_count"] == 0 for case in cases
        ),
        "all_content_paths_produced_delta": bool(content_cases)
        and all(
            case["first_content_ms"] is not None and case["content_chars"] > 0
            for case in content_cases
        ),
        "tool_call_produced": bool(tool_cases)
        and all(
            case["tool_calls"] and case["finish"] == "tool_calls"
            for case in tool_cases
        ),
        "tool_result_produced_content": any(
            case["case"] == "content_after_tool_result"
            and case["first_content_ms"] is not None
            for case in cases
        ),
    }
    return {
        "schema_version": "eidolon_agent.llm_stream_modes.v1",
        "model": router.model_id,
        "configured_thinking": configured_model.thinking,
        "repeat": repeat,
        "checks": checks,
        "passed": all(checks.values()),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")

    report = asyncio.run(_run(args.repeat))
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
