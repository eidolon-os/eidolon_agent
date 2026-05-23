"""Async persona interaction and evolution worker."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from eidolon_agent.domain.personas.evolution import PersonaEvolutionEngine
from eidolon_agent.domain.personas.ports import (
    PersonaAuditPort,
    PersonaEventPort,
    PersonaInstanceStore,
)
from eidolon_agent.domain.personas.runtime_state import PersonaRuntimeStateStore
from eidolon_agent.domain.personas.types import (
    AttentionTarget,
    PersonaEvolutionEvent,
    PersonaInteractionEvent,
)

_log = logging.getLogger(__name__)


class PersonaEvolutionWorker:
    def __init__(
        self,
        *,
        instances: PersonaInstanceStore,
        runtime_state: PersonaRuntimeStateStore,
        evolution: PersonaEvolutionEngine,
        audit_port: PersonaAuditPort,
        event_port: PersonaEventPort,
    ) -> None:
        self._instances = instances
        self._runtime = runtime_state
        self._evolution = evolution
        self._audit = audit_port
        self._events = event_port
        self._queue: asyncio.Queue[PersonaInteractionEvent] = asyncio.Queue()
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def submit(self, event: PersonaInteractionEvent) -> None:
        self._queue.put_nowait(event)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="persona-evolution-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def drain_once(self) -> bool:
        try:
            event = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return False
        await self._handle(event)
        self._queue.task_done()
        return True

    async def join(self) -> None:
        await self._queue.join()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            event = await self._queue.get()
            try:
                await self._handle(event)
            except Exception:
                _log.exception("persona evolution event failed")
            finally:
                self._queue.task_done()

    async def _handle(self, event: PersonaInteractionEvent) -> None:
        lock = self._locks.setdefault(event.instance_id, asyncio.Lock())
        async with lock:
            await self._apply_runtime_state(event)
            if not event.template_id:
                return
            instance = await self._instances.load(
                event.tenant_id, event.user_id, event.instance_id
            )
            evo_events = _interaction_to_evolution_events(event)
            if not evo_events:
                return
            evolved, result = self._evolution.evolve(
                instance=instance,
                events=evo_events,
                dry_run=False,
            )
            if result.applied:
                # Bump version + persist together. SqlPersonaInstanceStore.save
                # wraps the row update + evolution_history append in one TX so
                # the worker cannot leave a half-applied state on crash.
                evolved = evolved.model_copy(
                    update={"overlay_version": instance.overlay_version + 1}
                )
                await self._instances.save(
                    evolved, reason=f"async_evolution:{event.kind}"
                )
                await self._audit.record_evolution(result)
                await self._events.publish_evolution_applied(
                    event.instance_id,
                    result.model_dump(mode="json"),
                )
                await self._events.publish_persona_updated(
                    event.instance_id,
                    {"reason": "async_evolution", "changes": result.model_dump(mode="json")["changes"]},
                )

    async def _apply_runtime_state(self, event: PersonaInteractionEvent) -> None:
        if event.kind == "turn_completed":
            await self._runtime.update(
                instance_id=event.instance_id,
                attention_target=AttentionTarget.USER,
                focus_score=0.65,
            )
        emotion = event.payload.get("emotion")
        if emotion:
            await self._runtime.update(
                instance_id=event.instance_id,
                emotion=str(emotion),
                emotion_delta=float(event.payload.get("emotion_delta", 0.15)),
            )


def _interaction_to_evolution_events(
    event: PersonaInteractionEvent,
) -> list[PersonaEvolutionEvent]:
    now = event.created_at or datetime.now(timezone.utc)
    out: list[PersonaEvolutionEvent] = []
    if event.kind == "positive_feedback_received":
        out.append(PersonaEvolutionEvent(kind=event.kind, source="interaction", created_at=now))
    for item in event.payload.get("evolution_events", ()):
        out.append(PersonaEvolutionEvent(kind=str(item), source=event.kind, created_at=now))
    return out

