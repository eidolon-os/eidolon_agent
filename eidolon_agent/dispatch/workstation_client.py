"""NATS-based workstation agent client.

Submits a task on ``agent.workstation.task.submit`` (request/reply) and
subscribes to ``agent.workstation.task.progress.<task_id>`` for streaming
updates until a terminal Progress.kind is received.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from eidolon_agent.core.errors import DependencyError
from eidolon_agent.core.types.dispatch import (
    DispatchHandle,
    ExternalTask,
    Progress,
    ProgressKind,
)
from eidolon_agent.infra.events.topics import Topics

_log = logging.getLogger(__name__)
_TERMINAL = {ProgressKind.SUCCESS, ProgressKind.FAILURE, ProgressKind.CANCELLED}


class NatsWorkstationClient:
    def __init__(self, event_bus, *, request_timeout_s: float = 5.0) -> None:
        self._bus = event_bus
        self._request_timeout_s = request_timeout_s

    async def submit(self, task: ExternalTask) -> DispatchHandle:
        try:
            reply = await self._bus.request(
                Topics.workstation_submit(),
                payload={
                    "task_id": task.id,
                    "tenant_id": task.tenant_id,
                    "user_id": task.user_id,
                    "natural_language": task.natural_language,
                    "structured": task.structured,
                    "priority": task.priority,
                },
                timeout_s=self._request_timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise DependencyError(f"workstation submit timed out for task {task.id}") from exc
        accepted_task_id = reply.get("task_id", task.id)
        return DispatchHandle(
            task_id=accepted_task_id,
            progress_subject=Topics.workstation_progress(accepted_task_id),
        )

    async def stream_progress(self, handle: DispatchHandle) -> AsyncIterator[Progress]:
        queue: asyncio.Queue[Progress] = asyncio.Queue()
        done = asyncio.Event()

        async def _handler(event) -> None:  # type: ignore[no-untyped-def]
            try:
                kind = ProgressKind(event.payload.get("kind", "running"))
            except ValueError:
                kind = ProgressKind.RUNNING
            prog = Progress(
                task_id=handle.task_id,
                kind=kind,
                note=event.payload.get("note", ""),
                payload=event.payload.get("payload") or {},
            )
            await queue.put(prog)
            if kind in _TERMINAL:
                done.set()

        unsub = await self._bus.subscribe(handle.progress_subject, _handler)
        try:
            while not done.is_set() or not queue.empty():
                try:
                    prog = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if done.is_set() and queue.empty():
                        return
                    continue
                yield prog
                if prog.kind in _TERMINAL:
                    return
        finally:
            await unsub()

    async def cancel(self, handle: DispatchHandle) -> bool:
        try:
            resp = await self._bus.request(
                Topics.workstation_submit() + ".cancel",
                payload={"task_id": handle.task_id},
                timeout_s=self._request_timeout_s,
            )
            return bool(resp.get("cancelled"))
        except asyncio.TimeoutError:
            return False

    async def health(self) -> bool:
        return await self._bus.health()


__all__ = ["NatsWorkstationClient"]
