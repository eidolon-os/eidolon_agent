"""Ports for asynchronous long-task submission."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.long_task import LongTaskRecord


class LongTaskQueueFullError(RuntimeError):
    """Raised when the local long-task queue cannot accept more work."""


@runtime_checkable
class LongTaskSubmitter(Protocol):
    async def submit(self, record: LongTaskRecord) -> None:
        """Accept a task for asynchronous execution."""
        ...


__all__ = ["LongTaskQueueFullError", "LongTaskSubmitter"]
