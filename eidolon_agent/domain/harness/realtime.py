"""Realtime harness policy and prompt-safe trace helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from eidolon_agent.core.types.tool import ToolSchema

HARNESS_POLICY_SOURCE = "realtime_agent_harness"


@dataclass(frozen=True, slots=True)
class HarnessBudget:
    memory_timeout_ms: int = 200
    history_timeout_ms: int = 50
    history_window: int = 4
    max_tool_iters: int = 4
    first_delta_budget_ms: int = 300
    message_budget_tokens: int = 1800
    tool_schema_budget_tokens: int = 800
    output_reserve_tokens: int = 500

    def to_metadata(self) -> dict[str, int]:
        return {
            "memory_timeout_ms": self.memory_timeout_ms,
            "history_timeout_ms": self.history_timeout_ms,
            "history_window": self.history_window,
            "max_tool_iters": self.max_tool_iters,
            "first_delta_budget_ms": self.first_delta_budget_ms,
            "message_budget_tokens": self.message_budget_tokens,
            "tool_schema_budget_tokens": self.tool_schema_budget_tokens,
            "output_reserve_tokens": self.output_reserve_tokens,
        }


@dataclass(frozen=True, slots=True)
class HarnessSnapshot:
    segment_kinds: tuple[str, ...] = ()
    budget: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    history: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    handoffs: tuple[dict[str, Any], ...] = ()

    def to_metadata(self) -> dict[str, Any]:
        return {
            "kind": "realtime_agent_harness",
            "segment_kinds": list(self.segment_kinds),
            "budget": dict(self.budget),
            "memory": dict(self.memory),
            "history": dict(self.history),
            "tools": dict(self.tools),
            "handoffs": [dict(item) for item in self.handoffs],
        }


class RealtimeAgentHarness:
    """Single harness for realtime turns.

    The class intentionally stays thin. It owns the prompt-facing runtime
    policy and the list of tool schemas visible to the LLM, while dispatch
    compatibility remains in ToolRegistry/ToolDispatcher.
    """

    def __init__(
        self,
        *,
        budget: HarnessBudget | None = None,
        hidden_tool_names: set[str] | None = None,
    ) -> None:
        self.budget = budget or HarnessBudget()
        self._hidden_tool_names = set(hidden_tool_names or {"submit_long_task"})

    def policy_prompt(self) -> str:
        return realtime_harness_policy_prompt()

    def visible_tool_schemas(self, schemas: list[ToolSchema]) -> list[ToolSchema]:
        return [schema for schema in schemas if schema.name not in self._hidden_tool_names]

    def tool_schema_budget(self, schemas: list[ToolSchema]) -> dict[str, Any]:
        tokens_by_name = {
            schema.name: _estimate_tool_schema_tokens(schema) for schema in schemas
        }
        total = sum(tokens_by_name.values())
        return {
            "schema_count": len(schemas),
            "schema_token_estimate": total,
            "schema_budget_tokens": self.budget.tool_schema_budget_tokens,
            "schema_budget_exceeded": total > self.budget.tool_schema_budget_tokens,
            "tokens_by_name": tokens_by_name,
        }

    def snapshot(
        self,
        *,
        segment_kinds: list[str],
        budget: dict[str, Any],
        memory: dict[str, Any],
        history: dict[str, Any],
        tools: list[str] | None = None,
        tool_budget: dict[str, Any] | None = None,
        handoffs: list[dict[str, Any]] | None = None,
    ) -> HarnessSnapshot:
        tools_snapshot = {"visible_names": list(tools or [])}
        if tool_budget:
            tools_snapshot.update(tool_budget)
        return HarnessSnapshot(
            segment_kinds=tuple(segment_kinds),
            budget=dict(budget),
            memory=dict(memory),
            history=dict(history),
            tools=tools_snapshot,
            handoffs=tuple(handoffs or ()),
        )


def realtime_harness_policy_prompt() -> str:
    return "\n".join(
        [
            "Realtime Agent Harness 策略：",
            "- 你是 realtime agent：优先完成当场对话、澄清和简短答复。",
            "- 当前用户 turn 是最高优先级；历史、记忆和摘要只作为辅助证据，不要盖过当前问题。",
            "- 能直接回答的问题，直接简洁回答，不要为了展示能力而调用工具。",
            "- 需要真实外部动作、查询、系统事件或异步处理时，必须调用合适工具；不要假装已经完成。",
            "- 对复杂、多步骤、耗时、需要外部执行或需要稍后回流结果的任务，调用 delegate_to_coworker 委托后台 cowork。",
            "- delegate_to_coworker 是复杂任务的唯一委托入口；cowork 是工具，不是另一套 realtime harness。",
            "- 调用 delegate_to_coworker 后，不要编造最终结果；只说明任务已交给 cowork，等待后续进度或结果。",
            "- 工具结果返回后，再基于真实结果总结给用户；工具失败时如实说明并给出可行下一步。",
            "- 首响优先：不要在当前回复里等待后台 cowork 完成。",
        ]
    )


def _estimate_tool_schema_tokens(schema: ToolSchema) -> int:
    payload = schema.to_openai_function()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return max(1, len(text) // 3)
