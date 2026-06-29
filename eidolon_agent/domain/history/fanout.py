"""Async fanout of completed turns to memory & emotion services via NATS."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from eidolon_sdk.memory import (
    ConversationTurnPayload,
    conversation_turn_subject,
    envelope_memory_payload,
)

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.identity import build_memory_actor_context
from eidolon_agent.core.types.topics import Topics
from eidolon_agent.domain.history.ports import MemoryTurnSubjectResolver

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryFanoutStatus:
    turn_id: str
    owner_id: str
    companion_id: str
    memory_realm_id: str
    memory_space_id: str
    subject: str | None
    state: str
    error: str | None = None
    recorded_at: str = ""


class MemoryFanoutStatusSink(Protocol):
    async def record_memory_fanout(self, status: MemoryFanoutStatus) -> None: ...


class HistoryFanout:
    def __init__(
        self,
        *,
        event_bus=None,
        memory_routes: MemoryTurnSubjectResolver | None = None,
        status_sink: MemoryFanoutStatusSink | None = None,
    ) -> None:
        self._bus = event_bus
        self._memory_routes = memory_routes
        self._status_sink = status_sink

    async def publish_turn(
        self,
        *,
        owner_id: str,
        companion_id: str,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        timestamp_iso: str,
        emotion_payload: dict | None = None,
        metadata: dict | None = None,
    ) -> MemoryFanoutStatus:
        status_subject: str | None = None
        if self._bus is None:
            status = self._status(
                owner_id=owner_id,
                companion_id=companion_id,
                memory_realm_id=memory_realm_id,
                memory_space_id=memory_realm_id,
                turn_id=turn_id,
                subject=None,
                state="skipped_no_bus",
                error=None,
            )
            await self._record_status(status)
            return status
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
        turn_payload = ConversationTurnPayload(
            turn_id=turn_id,
            context=context,
            timestamp=timestamp_iso,
            user_text=user_text,
            assistant_text=assistant_text,
            metadata=payload_metadata,
        )
        memory_payload = envelope_memory_payload(
            turn_payload,
            trace_id=turn_id,
        ).model_dump(mode="json")
        try:
            status_subject = (
                await self._memory_routes.render_turn_subject(context.memory_space_id)
                if self._memory_routes is not None
                else conversation_turn_subject(context.memory_space_id)
            )
            await self._bus.publish(
                Event(
                    subject=status_subject,
                    payload=memory_payload,
                    source="history.fanout",
                    metadata={"msg_id": turn_id},
                ),
                persistent=True,
            )
            status = self._status(
                owner_id=owner_id,
                companion_id=companion_id,
                memory_realm_id=memory_realm_id,
                memory_space_id=context.memory_space_id,
                turn_id=turn_id,
                subject=status_subject,
                state="published",
                error=None,
            )
        except Exception as exc:
            _log.exception("fanout to memory failed")
            status = self._status(
                owner_id=owner_id,
                companion_id=companion_id,
                memory_realm_id=memory_realm_id,
                memory_space_id=context.memory_space_id,
                turn_id=turn_id,
                subject=status_subject,
                state="publish_failed",
                error=str(exc),
            )
        await self._record_status(status)
        if emotion_payload:
            try:
                await self._bus.publish(
                    Event(
                        subject=Topics.emotion_turn(owner_id),
                        payload={
                            **turn_payload.model_dump(mode="json"),
                            "emotion": emotion_payload,
                        },
                        source="history.fanout",
                        metadata={"msg_id": turn_id},
                    ),
                    persistent=True,
                )
            except Exception:
                _log.exception("fanout to emotion failed")
        return status

    def _status(
        self,
        *,
        owner_id: str,
        companion_id: str,
        memory_realm_id: str,
        memory_space_id: str,
        turn_id: str,
        subject: str | None,
        state: str,
        error: str | None,
    ) -> MemoryFanoutStatus:
        return MemoryFanoutStatus(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            memory_space_id=memory_space_id,
            turn_id=turn_id,
            subject=subject,
            state=state,
            error=error,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )

    async def _record_status(self, status: MemoryFanoutStatus) -> None:
        if self._status_sink is None:
            return
        try:
            await self._status_sink.record_memory_fanout(status)
        except Exception:
            _log.exception("record memory fanout status failed")
