"""Turn — the atomic unit of work.

A Turn is one request/response cycle. It begins with a :class:`TurnInput` and
emits a stream of :class:`TurnEvent`. The same Turn pipeline runs three modes
(reactive / continuation / proactive) — they differ only in ``TurnInput.trigger``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from eidolon_agent.core.types.identity import CallerContext
from eidolon_agent.core.types.signal import SignalDigest


class TurnTrigger(str, Enum):
    USER_UTTERANCE = "user_utterance"  # standard incoming chat
    CONTINUATION = "continuation"  # follow-up in same conversation
    PROACTIVE = "proactive"  # agent-initiated (timer / signal / promise)
    SIGNAL_ONLY = "signal_only"  # PushSignal without a user message
    SYSTEM = "system"  # internal (warmup, health, etc.)


class TriageKind(str, Enum):
    """How the input was classified."""

    SIMPLE = "simple"  # short conversational reply
    COMPLEX_LONG = "complex_long"  # offload to workstation agent
    TOOL_DIRECT = "tool_direct"  # bypass LLM, run tool directly


class TurnStatus(str, Enum):
    OK = "ok"
    CANCELLED = "cancelled"
    ERRORED = "errored"
    HANDED_OFF = "handed_off"


class TurnEventKind(str, Enum):
    """Wire-protocol event kinds. Must stay in sync with eidolon.proto."""

    STATE = "state"
    DELTA = "delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CITATION = "citation"
    USAGE = "usage"
    DONE = "done"
    ERROR = "error"
    ACK = "ack"  # complex task accepted
    PROGRESS = "progress"  # streaming progress from workstation
    HANDOFF = "handoff"  # delegating to external agent


class FSMState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    REFLECTING = "reflecting"


@dataclass(frozen=True, slots=True)
class ProsodyHints:
    """Voice synthesis hints. Consumed by upstream TTS (LiveKit pipeline)."""

    rate: float = 1.0  # 0.5–2.0
    pitch: float = 0.0  # semitones offset
    emphasis: str = "moderate"  # none|reduced|moderate|strong
    pause_ms_after: int = 0
    ssml_fragment: str | None = None


@dataclass(frozen=True, slots=True)
class TurnInput:
    """Everything required to start a Turn."""

    turn_id: str  # uuid7 from caller; server dedupes
    conversation_id: str
    session_id: str
    caller: CallerContext
    trigger: TurnTrigger
    text: str | None = None
    realtime: SignalDigest | None = None
    attachments: tuple[dict, ...] = ()  # opaque; modality-specific
    metadata: dict = field(default_factory=dict)

    def is_user_turn(self) -> bool:
        return self.trigger in (TurnTrigger.USER_UTTERANCE, TurnTrigger.CONTINUATION)


@dataclass(frozen=True, slots=True)
class TurnEvent:
    """A single event emitted from a Turn. Maps 1:1 to a protobuf TurnEvent."""

    turn_id: str
    seq: int  # monotonic per-turn
    kind: TurnEventKind
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0  # epoch seconds; populated by emitter

    @classmethod
    def state(cls, turn_id: str, seq: int, state: FSMState, ts: float) -> TurnEvent:
        return cls(turn_id, seq, TurnEventKind.STATE, {"state": state.value}, ts)

    @classmethod
    def delta(
        cls,
        turn_id: str,
        seq: int,
        text: str,
        ts: float,
        *,
        prosody: ProsodyHints | None = None,
    ) -> TurnEvent:
        data: dict[str, Any] = {"text": text}
        if prosody is not None:
            data["prosody"] = prosody.__dict__
        return cls(turn_id, seq, TurnEventKind.DELTA, data, ts)

    @classmethod
    def done(
        cls, turn_id: str, seq: int, status: TurnStatus, ts: float, **extra: Any
    ) -> TurnEvent:
        return cls(turn_id, seq, TurnEventKind.DONE, {"status": status.value, **extra}, ts)

    @classmethod
    def error(
        cls, turn_id: str, seq: int, code: str, message: str, ts: float
    ) -> TurnEvent:
        return cls(turn_id, seq, TurnEventKind.ERROR, {"code": code, "message": message}, ts)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Final summary of a Turn for persistence / audit."""

    turn_id: str
    conversation_id: str
    status: TurnStatus
    triage_kind: TriageKind
    trigger: TurnTrigger
    seq_count: int
    started_at: datetime
    finished_at: datetime
    latency_first_delta_ms: int | None
    total_latency_ms: int
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd_micro: int = 0  # micro-USD to avoid float
    model: str | None = None
    error_code: str | None = None
    seq_in_conversation: int = 0
