"""Persona-level proactive behavior policy.

This module decides whether a persona *wants* to initiate contact. Scheduling,
transport publishing, and throttling remain outside personas.
"""

from __future__ import annotations

from eidolon_agent.personas.types import PersonaProactiveDecision, PersonaSnapshot


class PersonaProactivePolicy:
    def propose(self, *, snapshot: PersonaSnapshot) -> PersonaProactiveDecision | None:
        intimacy = snapshot.instance.behavioral_knobs.get("intimacy")
        if intimacy is None or intimacy.current < 0.2:
            return None
        hint = snapshot.runtime_state.to_prompt_hint()
        return PersonaProactiveDecision(
            instance_id=snapshot.instance.instance_id,
            user_id=snapshot.instance.user_id,
            intent="gentle_check_in",
            text="我在，想轻轻确认一下你现在还好吗？",
            style_hint=hint,
            cooldown_s=600,
        )
