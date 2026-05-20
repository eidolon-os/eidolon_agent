"""Per-turn finite-state machine. Drives the STATE events on the wire."""

from __future__ import annotations

import asyncio

from eidolon_agent.core.types.turn import FSMState

_VALID: dict[FSMState, frozenset[FSMState]] = {
    FSMState.IDLE: frozenset({FSMState.LISTENING, FSMState.THINKING}),
    FSMState.LISTENING: frozenset({FSMState.THINKING, FSMState.IDLE}),
    FSMState.THINKING: frozenset({FSMState.SPEAKING, FSMState.IDLE, FSMState.LISTENING}),
    FSMState.SPEAKING: frozenset({FSMState.REFLECTING, FSMState.LISTENING, FSMState.IDLE}),
    FSMState.REFLECTING: frozenset({FSMState.IDLE}),
}


class TurnFSM:
    def __init__(self, *, initial: FSMState = FSMState.IDLE) -> None:
        self._state = initial
        self._lock = asyncio.Lock()

    @property
    def state(self) -> FSMState:
        return self._state

    async def transition(self, new: FSMState) -> bool:
        async with self._lock:
            if new in _VALID.get(self._state, frozenset()):
                self._state = new
                return True
            return False

    async def force(self, new: FSMState) -> None:
        async with self._lock:
            self._state = new
