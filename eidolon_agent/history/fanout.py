"""Async fanout of completed turns to memory & emotion services via NATS.

Never blocks the calling turn pipeline — failures are logged + retried via the
``publish_outbox`` SQLite table. (Outbox flusher is a separate background task
spawned by the runtime; we just enqueue here.)
"""

from __future__ import annotations

import logging

from eidolon_agent.core.types.event import Event
from eidolon_agent.infra.events.topics import Topics
from eidolon_agent.infra.memory.discovery import MemoryRoutingTable

_log = logging.getLogger(__name__)


class HistoryFanout:
    def __init__(self, *, event_bus=None, memory_routes: MemoryRoutingTable | None = None) -> None:
        self._bus = event_bus
        self._memory_routes = memory_routes

    async def publish_turn(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        timestamp_iso: str,
        emotion_payload: dict | None = None,
    ) -> None:
        if self._bus is None:
            return
        memory_payload = {
            "turn_id": turn_id,
            "user_id": user_id,
            "session_id": session_id,
            "timestamp": timestamp_iso,
            "user_text": user_text,
            "assistant_text": assistant_text,
            "metadata": {"source": "eidolon-agent"},
        }
        try:
            memory_subject = (
                await self._memory_routes.render_turn_subject(user_id)
                if self._memory_routes is not None
                else Topics.memory_conversation_turn(user_id)
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
                        subject=Topics.emotion_turn(user_id),
                        payload={**memory_payload, "emotion": emotion_payload},
                        source="history.fanout",
                        metadata={"msg_id": turn_id},
                    ),
                    persistent=True,
                )
            except Exception:
                _log.exception("fanout to emotion failed")
