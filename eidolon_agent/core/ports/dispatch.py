"""DispatchPort — handoff to the external workstation agent."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.dispatch import DispatchHandle, ExternalTask, Progress


@runtime_checkable
class DispatchPort(Protocol):
    """Submit a complex task and stream back its progress."""

    async def submit(self, task: ExternalTask) -> DispatchHandle: ...

    def stream_progress(self, handle: DispatchHandle) -> AsyncIterator[Progress]:
        """Subscribe to the task's progress subject; closes when terminal status arrives."""
        ...

    async def cancel(self, handle: DispatchHandle) -> bool: ...

    async def health(self) -> bool: ...
