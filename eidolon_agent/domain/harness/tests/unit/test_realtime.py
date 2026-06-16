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


def test_visible_tool_schemas_hide_legacy_alias_only() -> None:
    harness = RealtimeAgentHarness()

    visible = harness.visible_tool_schemas(
        [
            _schema("get_time"),
            _schema("delegate_to_coworker"),
            _schema("submit_long_task"),
        ]
    )

    assert [schema.name for schema in visible] == ["get_time", "delegate_to_coworker"]


def test_snapshot_is_prompt_safe_shape() -> None:
    snapshot = RealtimeAgentHarness().snapshot(
        segment_kinds=["persona", "harness_policy", "current_user"],
        budget={"mode": "enabled"},
        memory={"degraded": False, "hit_count": 2},
        history={"message_count": 1},
        tools=["get_time", "delegate_to_coworker"],
        handoffs=[{"tool_name": "delegate_to_coworker", "task_id": "task-1"}],
    ).to_metadata()

    assert snapshot["kind"] == "realtime_agent_harness"
    assert snapshot["segment_kinds"] == ["persona", "harness_policy", "current_user"]
    assert snapshot["tools"]["visible_names"] == ["get_time", "delegate_to_coworker"]
    assert "secret prompt" not in str(snapshot)
