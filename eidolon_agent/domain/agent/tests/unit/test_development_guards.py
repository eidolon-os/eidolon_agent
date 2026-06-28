"""Development guard semantics for high-risk turn behavior."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.agent.turn import _memory_write_trace
from eidolon_agent.domain.runtime_policy import TurnRuntimePolicy
from tests.helpers import make_turn_input

pytestmark = pytest.mark.unit


def test_memory_write_shadow_records_candidate_without_fanout() -> None:
    ti = make_turn_input("以后叫我小满")

    trace = _memory_write_trace(
        ti=ti,
        assistant_text="好的，以后叫你小满。",
        policy=TurnRuntimePolicy.from_metadata(ti.metadata),
        mode="shadow",
    )

    assert trace["mode"] == "shadow"
    assert trace["trace_kind"] == "memory_write_intent"
    assert trace["durable_result"] == "async_memory_worker"
    assert trace["shadow_only"] is True
    assert trace["disposition"] == "semantic_upsert"
    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "shadow_only"


def test_memory_write_disabled_does_not_classify_or_fanout() -> None:
    ti = make_turn_input("以后叫我小满")

    trace = _memory_write_trace(
        ti=ti,
        assistant_text="好的，以后叫你小满。",
        policy=TurnRuntimePolicy.from_metadata(ti.metadata),
        mode="disabled",
    )

    assert trace["mode"] == "disabled"
    assert trace["disposition"] is None
    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "policy_disabled"
