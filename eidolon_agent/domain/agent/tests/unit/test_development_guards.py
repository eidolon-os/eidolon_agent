"""Development guard semantics for high-risk turn behavior."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.tool import ToolResult
from eidolon_agent.domain.agent.turn import (
    _memory_write_trace,
    _terminal_memory_write_ack,
)
from eidolon_agent.domain.runtime_policy import TurnRuntimePolicy
from tests.helpers import make_turn_input

pytestmark = pytest.mark.unit


def test_memory_write_shadow_records_candidate_without_fanout() -> None:
    ti = make_turn_input("以后叫我小满")

    trace = _memory_write_trace(
        ti=ti,
        assistant_text="好的，以后叫你小满。",
        policy=TurnRuntimePolicy.from_metadata(ti.metadata),
        mode="shadow",
    )

    assert trace["mode"] == "shadow"
    assert trace["trace_kind"] == "memory_write_intent"
    assert trace["durable_result"] == "async_memory_worker"
    assert trace["shadow_only"] is True
    assert trace["disposition"] == "semantic_upsert"
    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "shadow_only"


def test_memory_write_disabled_does_not_classify_or_fanout() -> None:
    ti = make_turn_input("以后叫我小满")

    trace = _memory_write_trace(
        ti=ti,
        assistant_text="好的，以后叫你小满。",
        policy=TurnRuntimePolicy.from_metadata(ti.metadata),
        mode="disabled",
    )

    assert trace["mode"] == "disabled"
    assert trace["disposition"] is None
    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "policy_disabled"


def test_memory_write_ignore_disposition_stops_fanout() -> None:
    ti = make_turn_input("好的")
    trace = _memory_write_trace(
        ti=ti,
        assistant_text="嗯嗯。",
        policy=TurnRuntimePolicy.from_metadata(ti.metadata),
    )

    assert trace["disposition"] == "ignore"
    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "low_signal"


def test_explicit_memory_tool_keeps_steward_projection_fanout() -> None:
    ti = make_turn_input("请记住我明天去北京")
    trace = _memory_write_trace(
        ti=ti,
        assistant_text="写入请求已经提交。",
        policy=TurnRuntimePolicy.from_metadata(ti.metadata),
        memory_tool_owned_turn=True,
    )

    assert trace["fanout_allowed"] is True
    assert trace["projection_only"] is True
    assert trace["skipped_reason"] is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("applied", "已经记下了。"),
        ("accepted", "记忆写入请求已提交，正在处理。"),
        ("retrying", "记忆写入请求已提交，正在处理。"),
    ],
)
def test_memory_write_ack_never_overstates_completion(
    status: str,
    expected: str,
) -> None:
    result = ToolResult(
        call_id="call-1",
        name="memory_assert_fact",
        ok=True,
        content={"status": status},
    )

    assert _terminal_memory_write_ack([result]) == expected
