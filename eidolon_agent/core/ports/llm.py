"""LLMPort — provider-agnostic streaming LLM abstraction.

The router (``brain.router``) implements the same Protocol — it routes to
concrete provider adapters and handles failover. Business code never imports
provider SDKs directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.llm import LLMDelta
from eidolon_agent.core.types.messages import ChatMessage
from eidolon_agent.core.types.tool import ToolSchema


@runtime_checkable
class LLMPort(Protocol):
    """A streaming LLM. All methods are async; ``stream`` yields incrementally."""

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSchema] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        request_id: str,
    ) -> AsyncIterator[LLMDelta]:
        """Yield :class:`LLMDelta` chunks until the model finishes or is cancelled.

        Cancellation contract: closing the iterator (``aclose``) MUST abort the
        upstream HTTP request and release the connection. Implementations
        should propagate ``asyncio.CancelledError`` cleanly.
        """
        ...

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        """Estimate tokens for budgeting. Should be cheap; allow approximation."""
        ...

    @property
    def model_id(self) -> str:
        """Provider-prefixed model identifier (e.g. ``openai:gpt-4o-mini``)."""
        ...
