"""Ports declared by ``domain/history``.

Concrete adapters live in ``infra/`` and are wired in by ``app/runtime``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MemoryTurnSubjectResolver(Protocol):
    """Renders the NATS subject for a user's memory turn writes.

    Implemented by ``infra/memory/discovery.MemoryRoutingTable``. We declare
    the Protocol here so ``domain/history/fanout`` can depend on the
    structural contract instead of importing an infra type.
    """

    async def render_turn_subject(self, user_id: str) -> str: ...
