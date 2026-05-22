"""Workstation handoff — publish a complex-task event and move on.

This replaces the former ``domain/dispatch/`` package + DispatchPort
protocol. The hot path no longer awaits progress events; the workstation
service consumes the publish on its own and pushes results back through
NATS for whatever subscriber owns them.
"""

from __future__ import annotations

import logging
import uuid

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.topics import Topics
from eidolon_agent.core.types.turn import TurnInput

_log = logging.getLogger(__name__)


async def submit_to_workstation(event_bus, ti: TurnInput) -> str:
    """Fire-and-forget submit. Returns the task_id we coined.

    Returns an empty string if no bus is wired (caller falls back to
    treating the turn as SIMPLE).
    """
    if event_bus is None:
        return ""
    task_id = uuid.uuid4().hex
    try:
        await event_bus.publish(
            Event(
                subject=Topics.workstation_submit(),
                payload={
                    "task_id": task_id,
                    "tenant_id": ti.caller.tenant_id,
                    "user_id": ti.caller.user_id,
                    "natural_language": ti.text or "",
                    "trace_id": ti.caller.trace_id,
                },
                trace_id=ti.caller.trace_id,
                source="agent.workstation",
            ),
            persistent=True,
        )
    except Exception:
        _log.exception("workstation submit failed for task %s", task_id)
        return ""
    return task_id
