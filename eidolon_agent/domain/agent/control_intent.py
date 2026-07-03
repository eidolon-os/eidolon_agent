"""Reflex-layer control-intent classification (brain side of Tier0).

Runs after the input guardrail and before triage, on every turn. Pure
lexicon rules from the shared SDK taxonomy — <1ms, no LLM. When the whole
utterance is a control command the turn engine short-circuits: a STOP never
reaches the LLM and returns a structured ``termination_cause="user_stop"``
DONE so the upstream channel can circuit-break TTS/rendering; a TOPIC_SWITCH
tags the TurnInput so the context compiler can fence off the previous topic.

The channel classifies partial ASR interims on its fast path and cancels
turns; this layer is the slow-path confirmation on the final utterance —
both consume ``eidolon_sdk.biz.dialogue_control`` so they cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_sdk.biz.dialogue_control import (
    InterruptIntent,
    classify_control_intent,
)

# Reply spoken when the user asked us to stop. Deliberately empty: a stop
# means "be quiet", so the default is to emit no DELTA at all and let the
# DONE's termination_cause drive the upstream circuit-break.
STOP_ACKNOWLEDGEMENT = ""


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """What the turn engine should do with a classified utterance."""

    intent: InterruptIntent
    confidence: float
    reason: str
    # True → do not run the LLM; finish the turn with termination_cause.
    short_circuit: bool

    @property
    def termination_cause(self) -> str | None:
        return "user_stop" if self.short_circuit else None


class ControlIntentClassifier:
    """Deterministic reflex classifier over the shared lexicons.

    A second-stage low-latency LLM confirmation can be slotted in behind the
    same interface (see triage's LLM-router plan); the rule layer stays as
    the always-available degradation path.
    """

    def classify(self, text: str | None) -> ControlDecision:
        if not text or not text.strip():
            return ControlDecision(
                intent=InterruptIntent.UNCERTAIN,
                confidence=0.0,
                reason="empty_text",
                short_circuit=False,
            )
        result = classify_control_intent(text)
        return ControlDecision(
            intent=result.intent,
            confidence=result.confidence,
            reason=result.reason,
            # Only a whole-utterance hard stop short-circuits the turn.
            # BACKCHANNEL/NOISE would normally be filtered by the channel
            # before a turn starts; if one does arrive as a turn we still
            # answer it (cheap, and safer than silently ignoring the user).
            short_circuit=result.intent is InterruptIntent.HARD_STOP,
        )
