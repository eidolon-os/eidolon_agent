"""ContextSegment — the unit of LLM context.

ContextCompiler runs every Provider concurrently, collects ``ContextSegment``
batches, and assembles them into a token-budgeted prompt. Lower-weight
segments are pruned first when the budget is tight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from eidolon_agent.core.types.messages import ChatMessage


class SegmentType(str, Enum):
    SYSTEM = "system"
    PERSONA = "persona"  # identity card, speech style, taboos
    SOUL = "soul"  # evolving soul markdown
    MEMORY = "memory"  # long-term recall
    HISTORY = "history"  # recent turns
    MINDSTATE = "mindstate"  # mood/energy/attention/bond — natural language hint
    REALTIME = "realtime"  # current signals (emotion/prosody/vision)
    TOOL_HINT = "tool_hint"  # short notes about available tools
    USER_INPUT = "user_input"  # current user message
    CITATION = "citation"  # explicit citations for grounding


class SegmentWeight(int, Enum):
    """Pruning order — higher weights are kept; lower are dropped first."""

    LOW = 10
    NORMAL = 50
    HIGH = 80
    CRITICAL = 100  # never pruned (identity / safety / current input)


@dataclass(frozen=True, slots=True)
class ContextSegment:
    """A single block of context attached to a Turn.

    Implementations should keep ``content`` compact; if the segment can be
    summarized, prefer summarization at provide-time over post-hoc compaction.
    """

    type: SegmentType
    weight: SegmentWeight
    content: str
    tokens: int  # tokenizer estimate; provider responsibility
    source: str  # provider name — for tracing
    tags: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompiledContext:
    """The output of :class:`ContextCompiler`. Ready to be fed to the LLM."""

    segments: tuple[ContextSegment, ...]
    messages: tuple[ChatMessage, ...]  # final message list for LLM
    total_tokens: int
    budget: int
    pruned_count: int = 0  # number of segments dropped due to budget
    degraded_providers: tuple[str, ...] = ()  # providers that soft-timed-out
