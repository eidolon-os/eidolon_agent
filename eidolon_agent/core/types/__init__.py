"""Shared data types used across all layers.

Hot-path types (StreamDelta, TurnEvent, ContextSegment) use
``@dataclass(slots=True, frozen=True)`` for speed; domain models that cross
protocol boundaries (Profile, Memory items, Settings) use
``pydantic.BaseModel`` for validation + serialization.

Naming convention: every type is re-exported from this module so callers
can simply ``from eidolon_agent.core.types import TurnEvent``. Internal modules
may import the submodule directly when they want a narrower surface.
"""

from __future__ import annotations

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason, LLMUsage
from eidolon_agent.core.types.memory import (
    MemoryHit,
    MemoryItem,
    MemoryKind,
    MemoryQueryPlan,
    MemoryScope,
    MemoryWriteDisposition,
    MemoryWriteDispositionKind,
    MemoryWritePolicy,
    classify_memory_write,
)
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.signal import RealtimeSignal, SignalDigest, SignalModality
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema
from eidolon_agent.core.types.trace import (
    TRACE_SCHEMA_VERSION,
    LatencyBreakdown,
    PersonaTrace,
    PrivacyTrace,
    ToolTrace,
    TurnTrace,
)
from eidolon_agent.core.types.turn import (
    FSMState,
    ProsodyHints,
    TriageKind,
    TurnEvent,
    TurnEventKind,
    TurnInput,
    TurnResult,
    TurnStatus,
    TurnTrigger,
)

__all__ = [  # noqa: RUF022 - grouped by domain rather than alphabetical for readability
    # event
    "Event",
    # identity
    "CallerContext",
    "CallerKind",
    "Identity",
    # llm
    "LLMDelta",
    "LLMFinishReason",
    "LLMUsage",
    # memory
    "MemoryHit",
    "MemoryItem",
    "MemoryKind",
    "MemoryQueryPlan",
    "MemoryScope",
    "MemoryWriteDisposition",
    "MemoryWriteDispositionKind",
    "MemoryWritePolicy",
    "classify_memory_write",
    # messages
    "ChatMessage",
    "MessageRole",
    # signal
    "RealtimeSignal",
    "SignalDigest",
    "SignalModality",
    # tool
    "Permission",
    "ToolCall",
    "ToolResult",
    "ToolSchema",
    # trace
    "TRACE_SCHEMA_VERSION",
    "LatencyBreakdown",
    "PersonaTrace",
    "PrivacyTrace",
    "ToolTrace",
    "TurnTrace",
    # turn
    "FSMState",
    "ProsodyHints",
    "TriageKind",
    "TurnEvent",
    "TurnEventKind",
    "TurnInput",
    "TurnResult",
    "TurnStatus",
    "TurnTrigger",
]
