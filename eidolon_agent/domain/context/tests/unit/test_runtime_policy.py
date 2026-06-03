"""Runtime privacy policy decisions."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.runtime_policy import TurnRuntimePolicy

pytestmark = pytest.mark.unit


def test_runtime_policy_normal_mode_allows_context_and_side_effects() -> None:
    policy = TurnRuntimePolicy.from_metadata({})

    assert policy.privacy.mode == "normal"
    assert policy.memory_recall_allowed is True
    assert policy.memory_write_allowed is True
    assert policy.history_context_allowed is True
    assert policy.mark_messages_private is False
    assert policy.post_turn_side_effects_allowed is True


def test_runtime_policy_private_mode_hides_history_and_blocks_writes() -> None:
    policy = TurnRuntimePolicy.from_metadata({"private": True})

    assert policy.privacy.mode == "private"
    assert policy.memory_recall_allowed is True
    assert policy.memory_write_allowed is False
    assert policy.history_context_allowed is False
    assert policy.mark_messages_private is True
    assert policy.post_turn_side_effects_allowed is False


def test_runtime_policy_temporary_mode_blocks_recall_and_persistence_side_effects() -> None:
    policy = TurnRuntimePolicy.from_metadata({"temporary": True})

    assert policy.privacy.mode == "temporary"
    assert policy.memory_recall_allowed is False
    assert policy.memory_write_allowed is False
    assert policy.history_context_allowed is False
    assert policy.mark_messages_private is True
    assert policy.post_turn_side_effects_allowed is False
