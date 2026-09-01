"""Architecture lock: Agent validates commits and never classifies control text."""

from __future__ import annotations

import inspect

from eidolon_agent.domain.agent import committed_turn, turn


def test_agent_only_validates_provider_neutral_commitment() -> None:
    validation_source = inspect.getsource(committed_turn)
    turn_source = inspect.getsource(turn.TurnEngine.run)

    assert "classify_control_intent" not in validation_source
    assert "CommittedTurnDecision.from_metadata" in validation_source
    assert "matches_text" in validation_source
    assert "HARD_STOP" not in turn_source
    assert "TOPIC_SWITCH" not in turn_source
    assert "topic_switch" not in turn_source
