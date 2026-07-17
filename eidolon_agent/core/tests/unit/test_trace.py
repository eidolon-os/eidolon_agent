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
        commitment_context_trace={
            "commitment_ids": ["commitment-1"],
            "context_injected": True,
        },
        memory_write_trace={"disposition": "semantic_upsert", "fanout_allowed": True},
        tool_trace=[
            ToolTrace(call_id="tc1", name="delegate_to_coworker", ok=True, latency_ms=2)
        ],
        persona=PersonaTrace(companion_id="inst", genome_id="tpl"),
        trace_id="trace-abc",
        control_intent="hard_stop",
        termination_cause="user_stop",
        usage={"tokens_in": 10, "tokens_out": 5},
    ).to_metadata()

    assert trace["schema_version"] == TRACE_SCHEMA_VERSION
    assert trace["boundary"] == "eidolon_agent.brain"
    assert trace["turn"]["turn_id"] == "t1"
    assert trace["turn"]["trace_id"] == "trace-abc"
    assert trace["turn"]["control_intent"] == "hard_stop"
    assert trace["turn"]["termination_cause"] == "user_stop"
    assert trace["latency"]["compile_ms"] == 20
    assert trace["memory_trace"]["hit_ids"] == ["m1"]
    assert trace["commitment_context_trace"]["commitment_ids"] == [
        "commitment-1"
    ]
    assert trace["memory_write_trace"]["disposition"] == "semantic_upsert"
    assert trace["tool_trace"][0]["name"] == "delegate_to_coworker"
    assert "prompt" not in str(trace).lower()


def test_turn_trace_control_fields_default_none() -> None:
    trace = TurnTrace(
        turn_id="t1",
        conversation_id="c1",
        status="ok",
        trigger="user_utterance",
        triage="simple",
        caller_kind="web_chat",
        model="fake",
        latency=LatencyBreakdown(),
    ).to_metadata()
    assert trace["turn"]["control_intent"] is None
    assert trace["turn"]["termination_cause"] is None
