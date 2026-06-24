"""ToolPort — a callable capability the LLM can invoke."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.identity import CallerContext
from eidolon_agent.core.types.tool import ToolCall, ToolResult, ToolSchema


@dataclass(slots=True)
class ToolInvocationContext:
    """Per-call context provided by the dispatcher."""

    caller: CallerContext
    turn_id: str
    conversation_id: str | None = None
    session_id: str | None = None
    user_text: str | None = None
    # The persona (template id) currently responding. Used as the memory
    # persona partition key for side-effecting memory writes.
    persona_id: str | None = None
    dry_run: bool = False


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
