"""Lifecycle hooks — pure routing, no business logic.

The executor walks registered :class:`HookPort` instances at each lifecycle
event (PRE_TURN, POST_COMPILE, PRE_LLM, POST_LLM, PRE_TOOL, POST_TOOL,
POST_TURN, ON_ERROR), passing a :class:`HookPayload`. A hook may mutate the
payload, short-circuit (skip remaining stages), or abort the turn entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class HookEvent(str, Enum):
    PRE_TURN = "pre_turn"
    POST_COMPILE = "post_compile"
    PRE_LLM = "pre_llm"
    POST_LLM = "post_llm"
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    POST_TURN = "post_turn"
    ON_ERROR = "on_error"
    ON_SIGNAL = "on_signal"
    ON_IDLE = "on_idle"
    ON_EVOLVE = "on_evolve"


class HookOutcome(str, Enum):
    CONTINUE = "continue"
    MUTATE = "mutate"  # payload was modified; pipeline continues
    SHORT_CIRCUIT = "short_circuit"  # skip remaining hooks at this stage
    ABORT = "abort"  # abort the entire turn


@dataclass(slots=True)
class HookPayload:
    """Mutable payload threaded through the hook chain.

    Hooks MAY mutate ``data`` in place (returning ``MUTATE``) for performance;
    immutability across the pipeline is enforced socially via code review.
    """

    event: HookEvent
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HookResult:
    outcome: HookOutcome
    response: dict[str, Any] | None = None  # used for SHORT_CIRCUIT / ABORT
    reason: str | None = None


@runtime_checkable
class HookPort(Protocol):
    """A single registered hook. Keep handlers fast — they sit on the hot path."""

    @property
    def event(self) -> HookEvent: ...

    @property
    def priority(self) -> int:
        """Lower runs first. Recommended ranges: <0 system, 0–99 framework, 100+ user."""
        ...

    @property
    def name(self) -> str: ...

    async def handle(self, payload: HookPayload) -> HookResult: ...
