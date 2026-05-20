"""NATS publishers for eidolon-memory writes (turns and KG commands)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from eidolon_agent.core.types.event import Event
from eidolon_agent.events.topics import Topics

_log = logging.getLogger(__name__)


class MemoryNatsPublisher:
    def __init__(self, *, event_bus) -> None:
        self._bus = event_bus

    async def publish_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        metadata: dict | None = None,
    ) -> None:
        payload = {
            "turn_id": turn_id,
            "user_id": user_id,
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user_text": user_text,
            "assistant_text": assistant_text,
            "metadata": metadata or {"source": "eidolon-agent"},
        }
        await self._bus.publish(
            Event(
                subject=Topics.memory_conversation_turn(user_id),
                payload=payload,
                source="memory.nats_pub",
                metadata={"msg_id": turn_id},
            ),
            persistent=True,
        )

    async def publish_kg_add(
        self,
        *,
        user_id: str,
        subject: str,
        predicate: str,
        object_: str,
        confidence: float = 0.9,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> None:
        payload = {
            "command": "kg_add_triple",
            "user_id": user_id,
            "subject": subject,
            "predicate": predicate,
            "object": object_,
            "confidence": confidence,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
        }
        await self._bus.publish(
            Event(
                subject=Topics.memory_cmd(user_id),
                payload=payload,
                source="memory.nats_pub",
            ),
            persistent=True,
        )
