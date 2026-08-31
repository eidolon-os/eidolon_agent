"""Memory types — interface with the external eidolon-memory service.

eidolon-memory exposes MCP for synchronous reads and NATS JetStream for
asynchronous writes. We mirror its terminology where possible (wing/room/drawer
are eidolon-memory internals — we don't surface them here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal


class MemoryKind(StrEnum):
    FRAGMENT = "fragment"
    FACT = "fact"
    PREFERENCE = "preference"
    PROMISE = "promise"
    EPISODE = "episode"


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """A memory record to be written. Equivalent to a memory-service fragment."""

    content: str
    kind: MemoryKind
    importance: int = 3  # 1..5
    confidence: float = 0.9
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A retrieved memory record returned by the memory service."""

    id: str
    content: str
    kind: MemoryKind
    similarity: float
    memory_time: datetime | None = None
    memory_time_source: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryRecallResult:
    """Prompt-ready memory recall result.

    ``degraded_reason`` is for operator traces, not prompt injection.
    """

    context: str = ""
    hits: list[MemoryHit] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    kg_triples: list[dict] = field(default_factory=list)
    diagnostics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActiveCommitment:
    """One current Realm-bound commitment exposed to companion context."""

    commitment_id: str
    promisor: str
    predicate: Literal["promised", "committed_to", "planned_to"]
    action: str
    status: Literal["proposed", "confirmed"]
    beneficiaries: tuple[str, ...] = ()
    participants: tuple[str, ...] = ()
    condition: str | None = None
    due_at: str | None = None
    revision: int = 1
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ActiveCommitmentReadResult:
    """Bounded product read result; terminal commitments are never included."""

    commitments: list[ActiveCommitment] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    degraded: bool = False
    degraded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryQueryPlan:
    """How the strategy plans to query memory for a single turn."""

    working_k: int = 20
    episodic_query: str = ""
    episodic_k: int = 3
    semantic_query: str = ""
    semantic_k: int = 5
    promise_force: bool = True
    voice: bool = True  # honour memory-service 50ms KG sub-budget
    kg_subjects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryWritePolicy:
    write_on_turn: bool = True
    write_on_reflection: bool = True
    dedup_threshold: float = 0.92
    conflict_resolver: Literal["newer_wins", "llm_merge", "keep_both"] = "newer_wins"
