"""Realtime harness policy and prompt-safe trace helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eidolon_agent.core.types.tool import ToolSchema

HARNESS_POLICY_SOURCE = "realtime_agent_harness"


@dataclass(frozen=True, slots=True)
class HarnessBudget:
    memory_timeout_ms: int = 200
    history_timeout_ms: int = 50
    history_window: int = 20
    max_tool_iters: int = 4
    first_delta_budget_ms: int = 300

    def to_metadata(self) -> dict[str, int]:
        return {
            "memory_timeout_ms": self.memory_timeout_ms,
            "history_timeout_ms": self.history_timeout_ms,
            "history_window": self.history_window,
            "max_tool_iters": self.max_tool_iters,
            "first_delta_budget_ms": self.first_delta_budget_ms,
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

    def snapshot(
        self,
        *,
        segment_kinds: list[str],
        budget: dict[str, Any],
        memory: dict[str, Any],
        history: dict[str, Any],
        tools: list[str] | None = None,
        handoffs: list[dict[str, Any]] | None = None,
    ) -> HarnessSnapshot:
        return HarnessSnapshot(
            segment_kinds=tuple(segment_kinds),
            budget=dict(budget),
            memory=dict(memory),
            history=dict(history),
            tools={"visible_names": list(tools or [])},
            handoffs=tuple(handoffs or ()),
        )


def realtime_harness_policy_prompt() -> str:
    return "\n".join(
        [
            "Realtime Agent Harness 策略：",
            "- 你是 realtime agent：优先完成当场对话、澄清和简短答复。",
            "- 能直接回答的问题，直接简洁回答，不要为了展示能力而调用工具。",
            "- 需要真实外部动作、查询、系统事件或异步处理时，必须调用合适工具；不要假装已经完成。",
            "- 对复杂、多步骤、耗时、需要外部执行或需要稍后回流结果的任务，调用 delegate_to_coworker 委托后台 cowork。",
            "- delegate_to_coworker 是复杂任务的唯一委托入口；cowork 是工具，不是另一套 realtime harness。",
            "- 调用 delegate_to_coworker 后，不要编造最终结果；只说明任务已交给 cowork，等待后续进度或结果。",
            "- 工具结果返回后，再基于真实结果总结给用户；工具失败时如实说明并给出可行下一步。",
            "- 首响优先：不要在当前回复里等待后台 cowork 完成。",
        ]
    )
