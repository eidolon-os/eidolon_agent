"""Drain the Agent's durable turn outbox into the existing Memory JetStream."""

from __future__ import annotations

import asyncio
import logging

from eidolon_agent.core.types.event import Event
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore

log = logging.getLogger(__name__)


class MemoryTurnDispatcher:
    def __init__(self, store: AgentRuntimeStore, event_bus, *, batch_size: int = 100) -> None:
        self._outbox = store.memory_turn_outbox
        self._bus = event_bus
        self._batch_size = batch_size

    async def dispatch_once(self) -> int:
        rows = await self._outbox.pending_batch(limit=self._batch_size)
        published = 0
        by_subject: dict[str, list] = {}
        for row in rows:
            by_subject.setdefault(row.subject, []).append(row)
        for subject_rows in by_subject.values():
            for row in subject_rows:
                try:
                    await self._bus.publish(
                        Event(
                            subject=row.subject,
                            payload=row.payload,
                            trace_id=row.trace_id,
                            source="agent.memory_turn_outbox",
                            metadata={"msg_id": row.turn_id},
                        ),
                        persistent=True,
                    )
                except Exception as exc:
                    await self._outbox.mark_failed(
                        row.turn_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    # Corrections must not overtake older turns in the same Realm.
                    break
                await self._outbox.mark_published(row.turn_id)
                published += 1
        return published


async def run_memory_turn_dispatcher(store: AgentRuntimeStore, event_bus) -> None:
    dispatcher = MemoryTurnDispatcher(store, event_bus)
    while True:
        try:
            published = await dispatcher.dispatch_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("memory turn dispatcher iteration failed")
            published = 0
        if published == 0:
            await asyncio.sleep(0.05)


__all__ = ["MemoryTurnDispatcher", "run_memory_turn_dispatcher"]
