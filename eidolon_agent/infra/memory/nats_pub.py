"""NATS publishers for eidolon-memory writes (turns and KG commands)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from eidolon_sdk.memory import (
    ConversationTurnPayload,
    KgAddTripleCommand,
    UserConfirmedFactCommand,
    conversation_turn_subject,
    memory_command_subject,
)

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.identity import (
    build_memory_actor_context,
    build_memory_space_id,
)
from eidolon_agent.infra.memory.discovery import MemoryRoutingTable

_log = logging.getLogger(__name__)


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
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            user_text=owner_text,
            assistant_text=assistant_text,
            metadata=metadata or {"source": "eidolon-agent"},
        ).model_dump(mode="json")
        subject = (
            await self._routes.render_turn_subject(context.memory_space_id)
            if self._routes is not None
            else conversation_turn_subject(context.memory_space_id)
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
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        subject: str,
        predicate: str,
        object_: str,
        confidence: float = 0.9,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        request_id = uuid4().hex
        memory_space_id = build_memory_space_id(memory_realm_id=memory_realm_id)
        payload = KgAddTripleCommand(
            request_id=request_id,
            memory_space_id=memory_space_id,
            issued_at=now,
            issuer="agent",
            subject=subject,
            predicate=predicate,
            object=object_,
            confidence=confidence,
            valid_from=valid_from.isoformat() if valid_from else None,
            valid_to=valid_to.isoformat() if valid_to else None,
            source_drawer_id=f"req:{request_id}",
            adapter_name="agent",
        ).model_dump(mode="json")
        nats_subject = (
            await self._routes.render_cmd_subject(memory_space_id)
            if self._routes is not None
            else memory_command_subject(memory_space_id)
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

    async def publish_confirmed_fact(
        self,
        *,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
        text: str,
        confidence: float = 0.99,
        tags: list[str] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        request_id = uuid4().hex
        memory_space_id = build_memory_space_id(memory_realm_id=memory_realm_id)
        payload = UserConfirmedFactCommand(
            request_id=request_id,
            memory_space_id=memory_space_id,
            issued_at=now,
            issuer="agent",
            text=text,
            wing="Wing_Profile",
            memory_type="profile",
            importance=5,
            confidence=confidence,
            tags=list(tags or []),
            scope="persona",
            visibility="all_devices",
            source_device_id=device_id or "",
            source_instance_id=companion_id or "",
            session_id=session_id or "",
        ).model_dump(mode="json")
        nats_subject = (
            await self._routes.render_cmd_subject(memory_space_id)
            if self._routes is not None
            else memory_command_subject(memory_space_id)
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
