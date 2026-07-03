"""Automatic evolution for companion personas."""

from __future__ import annotations

from datetime import datetime, timezone

from eidolon_agent.core.errors import EvolutionGuardError
from eidolon_agent.domain.personas.types import (
    BehavioralKnob,
    CompanionPersona,
    PersonaEvolutionChange,
    PersonaEvolutionEvent,
    PersonaEvolutionResult,
)


class PersonaEvolutionEngine:
    def evolve(
        self,
        *,
        instance: CompanionPersona,
        events: list[PersonaEvolutionEvent],
        dry_run: bool = False,
    ) -> tuple[CompanionPersona, PersonaEvolutionResult]:
        now = datetime.now(timezone.utc)
        knobs = dict(instance.behavioral_knobs)
        rule_timestamps = dict(instance.evolution_state.applied_rule_timestamps)
        changes: list[PersonaEvolutionChange] = []

        for event in events:
            for rule in instance.evolution_rules:
                if rule.event != event.kind:
                    continue
                previous = rule_timestamps.get(rule.id)
                if previous is not None and rule.cooldown_hours > 0:
                    elapsed = (now - previous).total_seconds() / 3600
                    if elapsed < rule.cooldown_hours:
                        continue
                knob_name = _target_knob(rule.target)
                old_knob = knobs.get(knob_name)
                if old_knob is None:
                    raise EvolutionGuardError(f"unknown behavioral knob: {knob_name}")
                new_knob = _apply_rule(old_knob, action=rule.action, amount=rule.amount)
                if new_knob.current == old_knob.current:
                    continue
                knobs[knob_name] = new_knob
                rule_timestamps[rule.id] = now
                changes.append(
                    PersonaEvolutionChange(
                        path=rule.target,
                        old=old_knob.current,
                        new=new_knob.current,
                        rule_id=rule.id,
                    )
                )

        result = PersonaEvolutionResult(
            companion_id=instance.companion_id,
            applied=bool(changes) and not dry_run,
            changes=tuple(changes),
            events=tuple(events),
            rationale="automatic persona evolution" if changes else "no matching evolution rule",
        )
        if dry_run or not changes:
            return instance, result

        new_state = instance.evolution_state.model_copy(
            update={
                "applied_rule_timestamps": rule_timestamps,
                "last_event_summary": ", ".join(e.kind for e in events),
            }
        )
        evolved = instance.model_copy(
            update={
                "behavioral_knobs": knobs,
                "evolution_state": new_state,
                "updated_at": now,
            }
        )
        return evolved, result


def _target_knob(target: str) -> str:
    prefix = "behavioral_knobs."
    if not target.startswith(prefix):
        raise EvolutionGuardError(f"evolution target must start with {prefix!r}: {target}")
    return target[len(prefix) :]


def _apply_rule(knob: BehavioralKnob, *, action: str, amount: float) -> BehavioralKnob:
    if action == "increase":
        requested = knob.current + amount
    elif action == "decrease":
        requested = knob.current - amount
    elif action == "set":
        requested = amount
    else:
        raise EvolutionGuardError(f"unknown evolution action: {action}")

    delta = requested - knob.current
    if abs(delta) > knob.step_limit:
        delta = knob.step_limit if delta > 0 else -knob.step_limit
    current = min(knob.max, max(knob.min, knob.current + delta))
    return knob.model_copy(update={"current": current, "last_changed_at": datetime.now(timezone.utc)})

