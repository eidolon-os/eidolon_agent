"""NATS publisher for natural conversation-turn observations."""

from __future__ import annotations

from datetime import UTC, datetime

from eidolon_memory_contracts import (
    ConversationTurnPayload,
    conversation_turn_subject,
    envelope_memory_payload,
)

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.turn_context import build_memory_actor_context
from eidolon_agent.infra.memory.discovery import MemoryRoutingTable


class MemoryNatsPublisher:
    def __init__(self, *, event_bus, routes: MemoryRoutingTable | None = None) -> None:
        self._bus = event_bus
        self._routes = routes

    async def publish_turn(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
        turn_id: str,
        owner_text: str,
        assistant_text: str,
        metadata: dict | None = None,
    ) -> None:
        context = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        payload = ConversationTurnPayload(
            turn_id=turn_id,
            context=context,
            timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            user_text=owner_text,
            assistant_text=assistant_text,
            metadata=metadata or {"source": "eidolon-agent"},
        )
        subject = (
            await self._routes.render_turn_subject(context.memory_space_id)
            if self._routes is not None
            else conversation_turn_subject(context.memory_space_id)
        )
        await self._bus.publish(
            Event(
                subject=subject,
                payload=envelope_memory_payload(payload, trace_id=turn_id).model_dump(mode="json"),
                source="memory.nats_pub",
                metadata={"msg_id": turn_id},
            ),
            persistent=True,
        )


__all__ = ["MemoryNatsPublisher"]
