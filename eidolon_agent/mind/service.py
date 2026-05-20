"""In-memory MindState service. One snapshot per AgentInstance.

Persistence to NATS KV (`mind.state.<instance_id>`) is opt-in: pass a
``kv_store`` to enable cross-process reads. By default the service is purely
in-process which is correct for desktop single-process deployment.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timezone

from eidolon_agent.core.types.mind import (
    Attention,
    AttentionTarget,
    Bond,
    Energy,
    MindState,
)


class MindStateService:
    def __init__(self, *, kv_store=None) -> None:
        self._states: dict[str, MindState] = {}
        self._lock = asyncio.Lock()
        self._kv = kv_store

    def snapshot(self, *, instance_id: str) -> MindState:
        return self._states.get(instance_id, MindState())

    async def tick(self, *, instance_id: str, now: datetime | None = None) -> MindState:
        now = now or datetime.now(timezone.utc)
        async with self._lock:
            cur = self._states.get(instance_id, MindState())
            new_state = MindState(
                mood=cur.mood.decayed(now),
                energy=cur.energy,
                attention=cur.attention,
                bond=cur.bond,
            )
            self._states[instance_id] = new_state
            return new_state

    async def apply_event(
        self,
        *,
        instance_id: str,
        emotion: str | None = None,
        delta: float = 0.0,
        attention_target: AttentionTarget | None = None,
        bond_xp_delta: int = 0,
    ) -> MindState:
        async with self._lock:
            cur = self._states.get(instance_id, MindState())
            new_mood = cur.mood
            if emotion is not None:
                new_val = max(0.0, getattr(cur.mood, emotion, 0.0) + delta)
                new_mood = dataclasses.replace(
                    cur.mood,
                    **{emotion: new_val},
                    intensity=min(1.0, cur.mood.intensity + abs(delta)),
                    updated_at=datetime.now(timezone.utc),
                )
            new_attention = cur.attention
            if attention_target is not None:
                new_attention = Attention(
                    target=attention_target,
                    focus_score=cur.attention.focus_score,
                    last_user_signal_at=datetime.now(timezone.utc),
                )
            new_bond = cur.bond
            if bond_xp_delta:
                xp = cur.bond.xp + bond_xp_delta
                level = cur.bond.level + xp // 100
                xp = xp % 100
                new_bond = Bond(level=min(10, level), xp=xp, milestones=cur.bond.milestones)
            new_state = MindState(
                mood=new_mood, energy=cur.energy, attention=new_attention, bond=new_bond
            )
            self._states[instance_id] = new_state
            return new_state

    def apply_event_sync(self, *, instance_id: str, emotion: str, delta: float) -> None:
        """Used by tools that can't easily await (e.g. inside a sync ToolPort)."""
        cur = self._states.get(instance_id, MindState())
        new_val = max(0.0, getattr(cur.mood, emotion, 0.0) + delta)
        new_mood = dataclasses.replace(
            cur.mood,
            **{emotion: new_val},
            intensity=min(1.0, cur.mood.intensity + abs(delta)),
            updated_at=datetime.now(timezone.utc),
        )
        self._states[instance_id] = dataclasses.replace(cur, mood=new_mood)

    # Convenience for tests
    def reset(self, *, instance_id: str) -> None:
        self._states.pop(instance_id, None)


__all__ = ["Energy", "MindStateService"]  # re-export Energy so callers don't need core.types
