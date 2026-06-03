"""High-level memory access strategy — plans queries and orchestrates writes.

Consumers should prefer this class over the raw port: it knows the three-layer
working/episodic/semantic model and applies it consistently.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eidolon_agent.core.types.memory import MemoryQueryPlan


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


class MemoryStrategy:
    def __init__(self, *, port) -> None:
        self._port = port

    def plan_for_turn(self, *, user_text: str, voice: bool) -> MemoryQueryPlan:
        return MemoryQueryPlan(
            working_k=20,
            episodic_query=user_text,
            episodic_k=3,
            semantic_query=user_text,
            semantic_k=5,
            promise_force=True,
            voice=voice,
        )

    async def retrieve(self, *, user_id: str, user_text: str, voice: bool):
        plan = self.plan_for_turn(user_text=user_text, voice=voice)
        timeout_s = 0.15 if voice else 0.25
        return await self._port.recall_context(
            user_id=user_id,
            query=user_text,
            plan=plan,
            timeout_s=timeout_s,
        )

    def classify_write(
        self,
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

    async def write_turn(self, *, user_id: str, session_id: str, turn_id: str, user_text: str, assistant_text: str) -> None:
        await self._port.write_turn(user_id, session_id, turn_id, user_text, assistant_text)

    async def forget(self, *, user_id: str, query: str) -> int:
        return await self._port.forget(user_id, query)


__all__ = [
    "MemoryStrategy",
    "MemoryWriteDisposition",
    "MemoryWriteDispositionKind",
]
