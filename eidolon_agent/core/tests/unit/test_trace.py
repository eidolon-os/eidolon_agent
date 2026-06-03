"""TurnTrace JSON contract."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types import (
    TRACE_SCHEMA_VERSION,
    LatencyBreakdown,
    PersonaTrace,
    ToolTrace,
    TurnTrace,
)

pytestmark = pytest.mark.unit


def test_turn_trace_metadata_shape_is_prompt_safe() -> None:
    trace = TurnTrace(
        turn_id="t1",
        conversation_id="c1",
        status="ok",
        trigger="user_utterance",
        triage="simple",
        caller_kind="livekit_voice",
        model="fake",
        latency=LatencyBreakdown(compile_ms=20, first_delta_ms=100, total_ms=180),
        context_ledger={"segments": [{"kind": "memory"}]},
        memory_trace={"hit_ids": ["m1"], "context_injected": True},
        tool_trace=[ToolTrace(call_id="tc1", name="get_time", ok=True, latency_ms=2)],
        persona=PersonaTrace(instance_id="inst", template_id="tpl"),
        usage={"tokens_in": 10, "tokens_out": 5},
    ).to_metadata()

    assert trace["schema_version"] == TRACE_SCHEMA_VERSION
    assert trace["boundary"] == "eidolon_agent.brain"
    assert trace["turn"]["turn_id"] == "t1"
    assert trace["latency"]["compile_ms"] == 20
    assert trace["memory_trace"]["hit_ids"] == ["m1"]
    assert trace["tool_trace"][0]["name"] == "get_time"
    assert "prompt" not in str(trace).lower()
