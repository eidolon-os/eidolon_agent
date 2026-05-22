"""Fast in-memory runtime state for personas."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from eidolon_agent.personas.types import (
    Attention,
    AttentionTarget,
    Energy,
    PersonaRuntimeState,
)


class PersonaRuntimeStateStore:
    def __init__(self) -> None:
        self._states: dict[str, PersonaRuntimeState] = {}
        self._lock = asyncio.Lock()

    async def snapshot(self, *, instance_id: str) -> PersonaRuntimeState:
        now = datetime.now(timezone.utc)
        async with self._lock:
            state = self._states.get(instance_id, PersonaRuntimeState()).decayed(now)
            self._states[instance_id] = state
            return state

    async def update(
        self,
        *,
        instance_id: str,
        emotion: str | None = None,
        emotion_delta: float = 0.0,
        energy_level: float | None = None,
        attention_target: AttentionTarget | None = None,
        focus_score: float | None = None,
    ) -> PersonaRuntimeState:
        now = datetime.now(timezone.utc)
        async with self._lock:
            current = self._states.get(instance_id, PersonaRuntimeState()).decayed(now)
            mood = current.mood
            if emotion is not None and hasattr(mood, emotion):
                old_value = float(getattr(mood, emotion))
                new_value = max(0.0, old_value + emotion_delta)
                mood = mood.model_copy(
                    update={
                        emotion: new_value,
                        "intensity": min(1.0, mood.intensity + abs(emotion_delta)),
                        "updated_at": now,
                    }
                )
            energy = current.energy
            if energy_level is not None:
                energy = Energy(
                    level=min(1.0, max(0.0, energy_level)),
                    circadian_phase=energy.circadian_phase,
                    refilled_at=now,
                )
            attention = current.attention
            if attention_target is not None:
                attention = Attention(
                    target=attention_target,
                    focus_score=attention.focus_score if focus_score is None else focus_score,
                    last_user_signal_at=now,
                )
            elif focus_score is not None:
                attention = attention.model_copy(update={"focus_score": focus_score})

            updated = PersonaRuntimeState(
                mood=mood,
                energy=energy,
                attention=attention,
                updated_at=now,
            )
            self._states[instance_id] = updated
            return updated

    async def reset(self, *, instance_id: str) -> None:
        async with self._lock:
            self._states.pop(instance_id, None)

