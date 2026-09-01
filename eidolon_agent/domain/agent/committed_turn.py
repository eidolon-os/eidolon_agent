"""Validation for Channel-owned committed turn metadata."""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_sdk.biz.dialogue_control import CommittedTurnDecision


@dataclass(frozen=True, slots=True)
class CommittedTurnValidation:
    valid: bool
    persistence_allowed: bool
    reason: str


def validate_committed_turn(
    metadata: object,
    *,
    text: str | None,
    input_modality: str,
) -> CommittedTurnValidation:
    """Resolve the authoritative persistence boundary for one input.

    Voice crosses a process boundary and therefore requires Channel's typed,
    transcript-bound commitment. Text is received synchronously by Agent and
    owns its commit boundary locally, so missing voice metadata must not break
    text/Admin callers.
    """

    if input_modality != "voice":
        return CommittedTurnValidation(
            valid=False,
            persistence_allowed=True,
            reason="synchronous_text_boundary",
        )

    try:
        decision = CommittedTurnDecision.from_metadata(metadata)
    except (TypeError, ValueError):
        return CommittedTurnValidation(
            False,
            False,
            "missing_or_invalid_turn_decision",
        )
    if not decision.matches_text(text):
        return CommittedTurnValidation(
            False,
            False,
            "committed_turn_transcript_mismatch",
        )
    return CommittedTurnValidation(True, True, "committed_turn_valid")
