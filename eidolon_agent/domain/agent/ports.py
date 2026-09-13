"""Ports for the agent runtime's collaborators outside this layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from eidolon_agent.core.types.turn import TurnEvent, TurnInput


class LiveTurnObserver(Protocol):
    """Something that watches a turn go past without being able to change it.

    Named here rather than imported from ``infra.observability`` because the
    direction matters: ``domain`` describes what it needs and ``infra`` supplies
    it. Importing the concrete board put a telemetry component on the import path
    of the agent's single entry point, which is the dependency inverted — and
    import-linter had been saying so since 2026-08-27.

    Deliberately one method. The whole of what :class:`CompanionAgent` asks of a
    board is "wrap this stream", and a port wider than its use would invite the
    layer back in through the same door.
    """

    def observe(
        self, turn_input: TurnInput, stream: AsyncIterator[TurnEvent]
    ) -> AsyncIterator[TurnEvent]:
        """Yield ``stream`` unchanged, keeping whatever it tracks current.

        Not ``async def``: the implementation is an async generator, so calling
        it returns the iterator rather than a coroutine to await.
        """
        ...


__all__ = ["LiveTurnObserver"]
