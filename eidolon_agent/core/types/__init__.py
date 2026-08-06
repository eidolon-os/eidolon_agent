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
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason, LLMUsage
from eidolon_agent.core.types.long_task import (
    CallbackStatus,
    LongTaskRecord,
    LongTaskStatus,
    owner_id_from_safe_key,
    parse_session_key,
    safe_owner_key,
    session_key_for,
    task_key_for,
)
from eidolon_agent.core.types.memory import (
    MemoryHit,
    MemoryItem,
    MemoryKind,
    MemoryQueryPlan,
    MemoryRecallResult,
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
    DevelopmentGuardTrace,
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
from eidolon_agent.core.types.turn_context import InputModality, TurnContext

__all__ = [  # noqa: RUF022 - grouped by domain rather than alphabetical for readability
    # event
    "Event",
    # turn context
    "InputModality",
    "TurnContext",
    # llm
    "LLMDelta",
    "LLMFinishReason",
    "LLMUsage",
    # long task
    "CallbackStatus",
    "LongTaskRecord",
    "LongTaskStatus",
    "owner_id_from_safe_key",
    "parse_session_key",
    "safe_owner_key",
    "session_key_for",
    "task_key_for",
    # memory
    "MemoryHit",
    "MemoryItem",
    "MemoryKind",
    "MemoryQueryPlan",
    "MemoryRecallResult",
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
    "DevelopmentGuardTrace",
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
