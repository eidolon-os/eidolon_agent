"""CompanionAgent — a concrete Agent that owns one AgentInstance worth of state.

This is the small façade that the transport layer talks to. The Turn pipeline
itself lives in :class:`TurnEngine`; here we just hold references and expose
``run_turn`` as the single entry point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from eidolon_agent.core.types.turn import TurnEvent, TurnInput
from eidolon_agent.domain.agent.turn import TurnEngine


class CompanionAgent:
    def __init__(self, *, companion_id: str, turn_engine: TurnEngine) -> None:
        self.companion_id = companion_id
        self._engine = turn_engine

    def run_turn(self, ti: TurnInput) -> AsyncIterator[TurnEvent]:
        return self._engine.run(ti)
