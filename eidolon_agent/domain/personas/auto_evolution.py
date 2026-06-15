"""Policy gate for automatic long-term persona evolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from eidolon_agent.domain.personas.types import (
    PersonaEvolutionProposal,
    PersonaInstance,
    PersonaObservation,
)


@dataclass(frozen=True)
class AutoEvolutionDecision:
    apply: bool
    reason: str


@dataclass(frozen=True)
class PersonaAutoEvolutionPolicy:
    """Conservative gate for automatically applying evolution proposals."""

    enabled: bool = True
    min_confidence: float = 0.65
    min_evidence_strength: float = 0.55
    max_abs_delta: float = 0.04

    def evaluate(
        self,
        *,
        instance: PersonaInstance,
        proposal: PersonaEvolutionProposal,
        evidence: list[PersonaObservation],
        now: datetime | None = None,
    ) -> AutoEvolutionDecision:
        if not self.enabled:
            return AutoEvolutionDecision(False, "auto evolution disabled")
        if proposal.status != "pending":
            return AutoEvolutionDecision(False, f"proposal status is {proposal.status}")
        if proposal.confidence < self.min_confidence:
            return AutoEvolutionDecision(False, "proposal confidence below auto threshold")
        if not evidence:
            return AutoEvolutionDecision(False, "proposal has no loaded evidence")
        if any(obs.strength < self.min_evidence_strength for obs in evidence):
            return AutoEvolutionDecision(False, "evidence strength below auto threshold")
        if not proposal.patches:
            return AutoEvolutionDecision(False, "proposal has no patches")

        kinds = {obs.kind for obs in evidence}
        now = now or datetime.now(timezone.utc)
        for patch in proposal.patches:
            if patch.type != "knob_delta":
                return AutoEvolutionDecision(False, f"patch type requires review: {patch.type}")
            if patch.delta is None:
                return AutoEvolutionDecision(False, f"patch missing delta: {patch.target}")
            if abs(patch.delta) > self.max_abs_delta:
                return AutoEvolutionDecision(False, f"patch delta too large: {patch.target}")
            if not patch.target.startswith("behavioral_knobs."):
                return AutoEvolutionDecision(False, f"patch target requires review: {patch.target}")
            knob_name = patch.target.removeprefix("behavioral_knobs.")
            knob = instance.behavioral_knobs.get(knob_name)
            if knob is None:
                return AutoEvolutionDecision(False, f"unknown knob: {knob_name}")
            if abs(patch.delta) > knob.step_limit:
                return AutoEvolutionDecision(False, f"patch exceeds knob step limit: {knob_name}")
            if knob.last_changed_at is not None and knob.cooldown_hours > 0:
                last = knob.last_changed_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                elapsed_hours = (now - last).total_seconds() / 3600
                if elapsed_hours < knob.cooldown_hours:
                    return AutoEvolutionDecision(False, f"knob cooldown active: {knob_name}")
            allowed, reason = _is_low_risk_knob_delta(knob_name, patch.delta, kinds)
            if not allowed:
                return AutoEvolutionDecision(False, reason)
        return AutoEvolutionDecision(True, "low-risk proposal auto-applied")


def _is_low_risk_knob_delta(
    knob_name: str,
    delta: float,
    evidence_kinds: set[str],
) -> tuple[bool, str]:
    if knob_name == "vulnerability":
        return False, "vulnerability changes require review"
    if knob_name == "intimacy":
        if delta > 0 and delta <= 0.03 and evidence_kinds <= {"positive_feedback_received"}:
            return True, "positive-feedback intimacy increase is low risk"
        return False, "intimacy change requires review"
    if knob_name == "grounding":
        if delta > 0 and "stressor_memory_recalled" in evidence_kinds:
            return True, "grounding increase for stress evidence is low risk"
        return False, "grounding change requires matching stress evidence"
    if knob_name == "structure":
        if delta > 0 and "goal_progress_shared" in evidence_kinds:
            return True, "structure increase for goal evidence is low risk"
        return False, "structure change requires matching goal evidence"
    if knob_name == "directiveness":
        if delta < 0 and evidence_kinds & {
            "user_requests_less_advice",
            "boundary_correction_received",
        }:
            return True, "directiveness decrease for boundary evidence is low risk"
        if delta > 0 and delta <= 0.03 and "goal_progress_shared" in evidence_kinds:
            return True, "small goal-directed directiveness increase is low risk"
        return False, "directiveness change requires review"
    if knob_name == "extraversion":
        if delta < 0 and evidence_kinds & {
            "user_requests_less_advice",
            "boundary_correction_received",
            "stressor_memory_recalled",
        }:
            return True, "extraversion decrease for quieter support is low risk"
        return False, "extraversion increase requires review"
    if knob_name == "imagination":
        if delta > 0 and "creative_project_recalled" in evidence_kinds:
            return True, "imagination increase for creative evidence is low risk"
        return False, "imagination change requires matching creative evidence"
    if knob_name == "playfulness":
        if delta > 0 and evidence_kinds & {
            "creative_project_recalled",
            "positive_feedback_received",
        }:
            return True, "playfulness increase has matching positive or creative evidence"
        if delta < 0 and "boundary_correction_received" in evidence_kinds:
            return True, "playfulness decrease for boundary evidence is low risk"
        return False, "playfulness change requires review"
    return False, f"knob requires review: {knob_name}"


__all__ = ["AutoEvolutionDecision", "PersonaAutoEvolutionPolicy"]
