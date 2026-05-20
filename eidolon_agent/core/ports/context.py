"""ContextProvider — pluggable context source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.context import ContextSegment, SegmentType, SegmentWeight
from eidolon_agent.core.types.turn import TurnInput


@dataclass(slots=True)
class ProviderContext:
    """Read-only state passed to every provider in a single compile pass."""

    turn_input: TurnInput
    now_ms: int
    token_budget_hint: int  # soft hint; compiler enforces hard budget


@runtime_checkable
class ContextProvider(Protocol):
    """A single source of :class:`ContextSegment` items.

    The compiler runs all providers concurrently with a soft per-provider
    deadline. Providers SHOULD return partial results on timeout rather than
    raising.
    """

    @property
    def name(self) -> str: ...

    @property
    def segment_type(self) -> SegmentType: ...

    @property
    def default_weight(self) -> SegmentWeight: ...

    @property
    def soft_timeout_s(self) -> float:
        """How long the compiler will wait before declaring this provider degraded."""
        ...

    async def provide(self, ctx: ProviderContext) -> list[ContextSegment]:
        """Produce zero or more segments. Empty list = nothing to contribute."""
        ...
