"""Async fanout of completed turns to memory & emotion services via NATS.

Never blocks the calling turn pipeline — failures are logged + retried via the
``publish_outbox`` SQLite table. (Outbox flusher is a separate background task
spawned by the runtime; we just enqueue here.)
"""

from __future__ import annotations

import logging

from eidolon_sdk.memory import ConversationTurnPayload, conversation_turn_subject

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.identity import build_memory_actor_context
from eidolon_agent.core.types.topics import Topics
from eidolon_agent.domain.history.ports import MemoryTurnSubjectResolver

_log = logging.getLogger(__name__)


class HistoryFanout:
    def __init__(
        self,
        *,
        event_bus=None,
        memory_routes: MemoryTurnSubjectResolver | None = None,
    ) -> None:
        self._bus = event_bus
        self._memory_routes = memory_routes

    async def publish_turn(
        self,
        *,
        owner_id: str,
        companion_id: str,
        memory_realm_id: str,
        device_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        timestamp_iso: str,
        emotion_payload: dict | None = None,
        metadata: dict | None = None,
    ) -> None:
        if self._bus is None:
            return
        payload_metadata = {
            "source": "eidolon-agent",
            "source_project": "eidolon_agent",
            "source_component": "history.fanout",
            "source_turn_id": turn_id,
            "owner_id": owner_id,
            "companion_id": companion_id,
            "memory_realm_id": memory_realm_id,
        }
        if metadata:
            payload_metadata.update(metadata)
        context = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        memory_payload = ConversationTurnPayload(
            turn_id=turn_id,
            context=context,
            timestamp=timestamp_iso,
            user_text=user_text,
            assistant_text=assistant_text,
            metadata=payload_metadata,
        ).model_dump(mode="json")
        try:
            memory_subject = (
                await self._memory_routes.render_turn_subject(context.memory_space_id)
                if self._memory_routes is not None
                else conversation_turn_subject(context.memory_space_id)
            )
            await self._bus.publish(
                Event(
                    subject=memory_subject,
                    payload=memory_payload,
                    source="history.fanout",
                    metadata={"msg_id": turn_id},
                ),
                persistent=True,
            )
        except Exception:
            _log.exception("fanout to memory failed (will retry via outbox)")
        if emotion_payload:
            try:
                await self._bus.publish(
                    Event(
                        subject=Topics.emotion_turn(owner_id),
                        payload={**memory_payload, "emotion": emotion_payload},
                        source="history.fanout",
                        metadata={"msg_id": turn_id},
                    ),
                    persistent=True,
                )
            except Exception:
                _log.exception("fanout to emotion failed")
