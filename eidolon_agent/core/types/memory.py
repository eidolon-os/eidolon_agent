"""Memory types — interface with the external eidolon-memory service.

eidolon-memory exposes MCP for synchronous reads and NATS JetStream for
asynchronous writes. We mirror its terminology where possible (wing/room/drawer
are eidolon-memory internals — we don't surface them here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class MemoryScope(str, Enum):
    ALL = "all"
    WORKING = "working"  # current session (handled locally by HistoryManager)
    EPISODIC = "episodic"  # recent days
    SEMANTIC = "semantic"  # facts/preferences/promises
    PROMISE = "promise"  # pending promises only


class MemoryKind(str, Enum):
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
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict = field(default_factory=dict)


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


@dataclass(frozen=True, slots=True)
class MemoryWritePolicy:
    write_on_turn: bool = True
    write_on_reflection: bool = True
    dedup_threshold: float = 0.92
    conflict_resolver: Literal["newer_wins", "llm_merge", "keep_both"] = "newer_wins"


class MemoryWriteDispositionKind(str, Enum):
    IGNORE = "ignore"
    EPISODIC_ONLY = "episodic_only"
    SEMANTIC_UPSERT = "semantic_upsert"
    PROMISE_CREATE = "promise_create"
    SENSITIVE_REQUIRES_CONSENT = "sensitive_requires_consent"


@dataclass(frozen=True, slots=True)
class MemoryWriteDisposition:
    kind: MemoryWriteDispositionKind
    reason: str

    def to_metadata(self) -> dict[str, str]:
        return {
            "memory_write_disposition": self.kind.value,
            "memory_write_reason": self.reason,
            "memory_policy_version": "agent_memory_policy.v1",
        }


def classify_memory_write(
    *,
    user_text: str,
    assistant_text: str = "",
) -> MemoryWriteDisposition:
    text = f"{user_text}\n{assistant_text}".lower()
    if any(k in text for k in ("身份证", "银行卡", "password", "密码", "住址")):
        return MemoryWriteDisposition(
            MemoryWriteDispositionKind.SENSITIVE_REQUIRES_CONSENT,
            "sensitive_personal_data",
        )
    if any(k in text for k in ("提醒我", "记得提醒", "remind me", "promise")):
        return MemoryWriteDisposition(
            MemoryWriteDispositionKind.PROMISE_CREATE,
            "explicit_promise_or_reminder",
        )
    is_question = any(k in text for k in ("?", "？", "吗", "哪里", "什么"))
    if any(k in text for k in ("我喜欢", "我不喜欢", "以后叫我", "call me", "i like", "i prefer")) or (
        not is_question and any(k in text for k in ("我住在", "我现在住在"))
    ):
        return MemoryWriteDisposition(
            MemoryWriteDispositionKind.SEMANTIC_UPSERT,
            "stable_preference_or_identity",
        )
    if any(k in text for k in ("今天", "昨天", "刚才", "today", "yesterday", "this morning")):
        return MemoryWriteDisposition(
            MemoryWriteDispositionKind.EPISODIC_ONLY,
            "episodic_event",
        )
    return MemoryWriteDisposition(MemoryWriteDispositionKind.IGNORE, "low_signal_chitchat")
