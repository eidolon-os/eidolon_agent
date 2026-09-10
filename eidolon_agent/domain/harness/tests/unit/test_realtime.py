"""RealtimeAgentHarness policy and schema exposure."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.tool import ToolSchema
from eidolon_agent.domain.harness import (
    HarnessBudget,
    RealtimeAgentHarness,
    realtime_harness_policy_prompt,
)

pytestmark = pytest.mark.unit


def _schema(name: str) -> ToolSchema:
    return ToolSchema(name=name, description=f"{name} desc", json_schema={"type": "object"})


def test_policy_prompt_names_realtime_and_coworker_tool_boundary() -> None:
    prompt = realtime_harness_policy_prompt()

    assert "realtime agent" in prompt
    assert "delegate_to_coworker" in prompt
    assert "cowork 是工具" in prompt
    assert "不要编造最终结果" in prompt
    assert "不要在当前回复里等待后台 cowork 完成" in prompt


def test_default_budget_is_realtime_safe() -> None:
    budget = HarnessBudget()

    assert budget.memory_timeout_ms == 200
    assert budget.history_timeout_ms == 50
    assert budget.history_window == 20
    assert budget.first_delta_budget_ms == 300
    assert budget.tool_schema_budget_tokens == 800
    assert budget.output_reserve_tokens == 500


def test_visible_tool_schemas_keep_only_prompt_visible_tools() -> None:
    harness = RealtimeAgentHarness()

    visible = harness.visible_tool_schemas(
        [
            _schema("emit_event"),
            _schema("delegate_to_coworker"),
        ]
    )

    assert [schema.name for schema in visible] == ["delegate_to_coworker"]


def test_visible_tool_schemas_can_override_hidden_tools() -> None:
    harness = RealtimeAgentHarness(hidden_tool_names=set())

    visible = harness.visible_tool_schemas([_schema("emit_event")])

    assert [schema.name for schema in visible] == ["emit_event"]


def test_snapshot_is_prompt_safe_shape() -> None:
    snapshot = RealtimeAgentHarness().snapshot(
        segment_kinds=["persona", "harness_policy", "current_user"],
        budget={"mode": "enabled"},
        memory={"degraded": False, "hit_count": 2},
        history={"message_count": 1},
        tools=["delegate_to_coworker"],
        tool_budget={"schema_token_estimate": 42},
        handoffs=[{"tool_name": "delegate_to_coworker", "task_id": "task-1"}],
    ).to_metadata()

    assert snapshot["kind"] == "realtime_agent_harness"
    assert snapshot["segment_kinds"] == ["persona", "harness_policy", "current_user"]
    assert snapshot["tools"]["visible_names"] == ["delegate_to_coworker"]
    assert snapshot["tools"]["schema_token_estimate"] == 42
    assert "secret prompt" not in str(snapshot)


def test_tool_schema_budget_estimates_prompt_visible_schema_cost() -> None:
    harness = RealtimeAgentHarness(budget=HarnessBudget(tool_schema_budget_tokens=1))

    budget = harness.tool_schema_budget([_schema("delegate_to_coworker")])

    assert budget["schema_count"] == 1
    assert budget["schema_token_estimate"] > 1
    assert budget["schema_budget_tokens"] == 1
    assert budget["schema_budget_exceeded"] is True
    assert set(budget["tokens_by_name"]) == {"delegate_to_coworker"}
