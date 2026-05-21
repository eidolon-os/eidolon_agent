"""NATS publishers for eidolon-memory writes (turns and KG commands)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from eidolon_agent.core.types.event import Event
from eidolon_agent.events.topics import Topics
from eidolon_agent.memory.discovery import MemoryRoutingTable

_log = logging.getLogger(__name__)


class MemoryNatsPublisher:
    def __init__(self, *, event_bus, routes: MemoryRoutingTable | None = None) -> None:
        self._bus = event_bus
        self._routes = routes

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
        subject = (
            await self._routes.render_turn_subject(user_id)
            if self._routes is not None
            else Topics.memory_conversation_turn(user_id)
        )
        await self._bus.publish(
            Event(
                subject=subject,
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
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        request_id = uuid4().hex
        payload = {
            "kind": "kg_add_triple",
            "request_id": request_id,
            "user_id": user_id,
            "issued_at": now,
            "issuer": "agent",
            "subject": subject,
            "predicate": predicate,
            "object": object_,
            "confidence": confidence,
            "valid_from": valid_from.isoformat() if valid_from else None,
            "valid_to": valid_to.isoformat() if valid_to else None,
            "source_drawer_id": f"req:{request_id}",
            "adapter_name": "agent",
        }
        nats_subject = (
            await self._routes.render_cmd_subject(user_id)
            if self._routes is not None
            else Topics.memory_cmd(user_id)
        )
        await self._bus.publish(
            Event(
                subject=nats_subject,
                payload=payload,
                source="memory.nats_pub",
                metadata={"msg_id": request_id},
            ),
            persistent=True,
        )
