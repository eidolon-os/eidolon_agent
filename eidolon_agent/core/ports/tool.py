"""ToolPort — a callable capability the LLM can invoke."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.tool import ToolCall, ToolResult, ToolSchema
from eidolon_agent.core.types.turn_context import InputModality, TurnContext


@dataclass(slots=True)
class ToolInvocationContext:
    """Per-call context provided by the dispatcher."""

    turn_context: TurnContext
    input_modality: InputModality
    turn_id: str
    conversation_id: str | None = None
    session_id: str | None = None
    user_text: str | None = None
    companion_id: str | None = None
    memory_realm_id: str | None = None
    dry_run: bool = False
    # Per-turn dynamic tools (e.g. device-capability tools) resolved by name
    # before the global registry. Keys are tool names.
    extra_tools: Mapping[str, ToolPort] | None = None
    # Tools denied for this companion (runtime_config_json). Enforced at dispatch
    # so a hallucinated denied name cannot actuate a real registered tool.
    denied_tools: frozenset[str] = field(default_factory=frozenset)


@runtime_checkable
class ToolPort(Protocol):
    """A registered tool."""

    @property
    def schema(self) -> ToolSchema: ...

    async def invoke(
        self,
        call: ToolCall,
        *,
        ctx: ToolInvocationContext,
    ) -> ToolResult:
        """Execute. MUST NOT raise on user-level failures — return ``ok=False``.

        Exceptions are reserved for programmer errors (bugs, invariant violations).
        Permission errors and timeouts should be wrapped in :class:`ToolResult`.
        """
        ...
