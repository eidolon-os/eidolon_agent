"""Committed turn validation tests."""

from __future__ import annotations

from eidolon_sdk.biz.dialogue_control import CommittedTurnDecision, TurnCommitBoundary

from eidolon_agent.domain.agent.committed_turn import validate_committed_turn


def _metadata(text: str) -> dict[str, object]:
    return CommittedTurnDecision.create(
        text=text,
        boundary=TurnCommitBoundary.FRAMEWORK_COMPLETED,
        eot_score=0.8,
    ).as_metadata()


def test_valid_commitment_matches_transcript_without_semantic_intent() -> None:
    validation = validate_committed_turn(
        _metadata("停一下"),
        text="停一下",
        input_modality="voice",
    )

    assert validation.valid is True
    assert validation.persistence_allowed is True
    assert "intent" not in _metadata("停一下")


def test_missing_or_malformed_commitment_is_non_authoritative() -> None:
    for metadata in (None, {}):
        validation = validate_committed_turn(
            metadata,
            text="停一下",
            input_modality="voice",
        )
        assert validation.valid is False
        assert validation.persistence_allowed is False


def test_stale_commitment_cannot_bind_to_next_transcript() -> None:
    validation = validate_committed_turn(
        _metadata("停一下"),
        text="帮我查天气",
        input_modality="voice",
    )

    assert validation.valid is False
    assert validation.persistence_allowed is False
    assert validation.reason == "committed_turn_transcript_mismatch"


def test_text_input_owns_a_synchronous_persistence_boundary() -> None:
    validation = validate_committed_turn(
        None,
        text="帮我查天气",
        input_modality="text",
    )

    assert validation.valid is False
    assert validation.persistence_allowed is True
    assert validation.reason == "synchronous_text_boundary"
