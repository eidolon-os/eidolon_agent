"""Brain-side policy for memory write candidates.

The memory service owns ingestion, deduplication, contradiction handling, and
promise scheduling. Agent only labels completed turns with enough intent and
source metadata for that service to make a good decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
    if any(k in text for k in ("我喜欢", "我不喜欢", "以后叫我", "call me", "i like", "i prefer")):
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


__all__ = [
    "MemoryWriteDisposition",
    "MemoryWriteDispositionKind",
    "classify_memory_write",
]
