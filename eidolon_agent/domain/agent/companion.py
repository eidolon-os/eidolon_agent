"""CompanionAgent — a concrete Agent that owns one AgentInstance worth of state.

This is the small façade that the transport layer talks to. The Turn pipeline
itself lives in :class:`TurnEngine`; here we just hold references and expose
``run_turn`` as the single entry point.

Being *the* single entry point is also why the live-turn board is attached here
and nowhere else. A turn in flight has to be observable for the map to draw a
conversation while it is happening, and there are exactly two places that could
know: inside the 640-line engine method, at five separate call sites that would
drift apart, or once around the stream every transport already consumes. The
second one cannot miss a turn and cannot change one — see
:mod:`eidolon_agent.infra.observability.live_turns`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from eidolon_agent.core.types.turn import TurnEvent, TurnInput
from eidolon_agent.domain.agent.turn import TurnEngine
from eidolon_agent.infra.observability import LiveTurnBoard


class CompanionAgent:
    def __init__(
        self,
        *,
        companion_id: str,
        turn_engine: TurnEngine,
        live_turns: LiveTurnBoard | None = None,
    ) -> None:
        self.companion_id = companion_id
        self._engine = turn_engine
        self._live_turns = live_turns

    def run_turn(self, ti: TurnInput) -> AsyncIterator[TurnEvent]:
        stream = self._engine.run(ti)
        board = self._live_turns
        if board is None:
            # No observer configured: the turn runs exactly as it always has.
            return stream
        return board.observe(ti, stream)
