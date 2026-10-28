"""Agent routes committed turns; Memory owns semantic write decisions."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.agent.turn import _memory_write_trace
from eidolon_agent.domain.runtime_policy import TurnRuntimePolicy
from tests.helpers import make_turn_input

pytestmark = pytest.mark.unit


def _trace(text: str, *, mode: str = "enabled", metadata: dict | None = None) -> dict:
    turn = make_turn_input(text)
    turn.metadata.update(metadata or {})
    return _memory_write_trace(
        ti=turn,
        policy=TurnRuntimePolicy.from_metadata(turn.metadata),
        mode=mode,
    )


def test_shadow_mode_observes_without_publishing() -> None:
    trace = _trace("任意自然表达", mode="shadow")

    assert trace["trace_kind"] == "memory_turn_observation"
    assert trace["ingest_policy"] == "semantic_steward"
    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "shadow_only"


def test_disabled_mode_does_not_publish() -> None:
    trace = _trace("任意自然表达", mode="disabled")

    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "policy_disabled"


@pytest.mark.parametrize(
    "text",
    [
        "好的",
        "我给书房的台灯起名叫云山831",
        "近期照明风格改成偏暖的颜色了",
        "Could you use a warmer light in the study?",
    ],
)
def test_wording_never_gates_a_committed_turn(text: str) -> None:
    trace = _trace(text)

    assert trace["fanout_allowed"] is True
    assert trace["skipped_reason"] is None


def test_empty_turn_is_not_published() -> None:
    trace = _trace("")

    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "empty_user_text"


def test_temporary_turn_respects_privacy_boundary() -> None:
    trace = _trace("本轮不应形成长期状态", metadata={"temporary": True})

    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "privacy_policy"


async def test_unknown_person_never_calls_memory_reads_or_writes():
    from dataclasses import replace
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from eidolon_agent.domain.agent.turn import TurnEngine
    from eidolon_agent.domain.context.compiler import ContextCompiler

    turn = make_turn_input("记住我的秘密")
    turn = replace(turn, context=replace(turn.context, memory_enabled=False))
    turn.metadata.update(memory_enabled=True, person_id="forged", temporary=False)
    trace = _memory_write_trace(ti=turn, policy=TurnRuntimePolicy.from_metadata(turn.metadata))
    assert trace["fanout_allowed"] is False
    assert trace["skipped_reason"] == "unidentified_person"
    compiler = object.__new__(ContextCompiler)
    compiler._memory = AsyncMock()
    assert await compiler._memory_recall(turn, timeout_s=0.1) is None
    assert await compiler._active_commitments(turn) is None
    assert compiler._memory.mock_calls == []
    engine = object.__new__(TurnEngine)
    engine._fanout = AsyncMock()
    await engine._publish_memory_observation(turn, datetime.now(UTC))
    assert engine._fanout.mock_calls == []


def _provisional_turn():
    from dataclasses import replace
    turn = make_turn_input("这轮可靠归属的原文")
    return replace(turn, context=replace(turn.context, memory_enabled=False,
        provisional_memory_save=True, person_id="candidate", person_revision=3,
        identity_epoch=2, source_session_id=turn.session_id))


async def test_provisional_save_publishes_frozen_source_without_private_reads():
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from eidolon_agent.domain.agent.turn import TurnEngine
    from eidolon_agent.domain.context.compiler import ContextCompiler

    turn = _provisional_turn()
    turn.metadata.update(person_id="forged", person_attribution="confirmed", memory_enabled=True)
    trace = _memory_write_trace(ti=turn, policy=TurnRuntimePolicy.from_metadata(turn.metadata))
    assert trace["fanout_allowed"] and trace["ingest_policy"] == "provisional_source"
    compiler = object.__new__(ContextCompiler)
    compiler._memory = AsyncMock()
    assert await compiler._memory_recall(turn, timeout_s=0.1) is None
    assert await compiler._active_commitments(turn) is None
    assert compiler._memory.mock_calls == []
    engine = object.__new__(TurnEngine)
    engine._fanout = AsyncMock()
    engine._genome_id = "g"
    await engine._publish_memory_observation(turn, datetime.now(UTC))
    payload = engine._fanout.publish_turn.call_args.kwargs
    assert payload["person_id"] == "candidate" and payload["identity_epoch"] == 2
    assert payload["session_id"] == turn.session_id
    assert payload["metadata"]["person_attribution"] == "provisional"
    assert payload["assistant_text"] == ""
    assert payload["user_text"] == turn.text


@pytest.mark.parametrize("change", [
    {"person_id": None}, {"person_revision": None}, {"identity_epoch": None},
    {"source_session_id": None}, {"memory_enabled": True}, {"provisional_memory_save": 1},
])
def test_provisional_save_cannot_be_an_unbound_or_broad_grant(change):
    from dataclasses import replace
    with pytest.raises(ValueError):
        replace(_provisional_turn().context, **change)


async def test_provisional_source_cannot_move_sessions_or_bypass_privacy():
    from dataclasses import replace
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from eidolon_agent.domain.agent.turn import TurnEngine

    turn = _provisional_turn()
    turn.metadata["temporary"] = True
    trace = _memory_write_trace(ti=turn, policy=TurnRuntimePolicy.from_metadata(turn.metadata))
    assert not trace["fanout_allowed"] and trace["skipped_reason"] == "privacy_policy"
    moved = replace(turn, session_id="other")
    trace = _memory_write_trace(ti=moved, policy=TurnRuntimePolicy.from_metadata({}))
    assert not trace["fanout_allowed"] and trace["skipped_reason"] == "provisional_session_mismatch"
    engine = object.__new__(TurnEngine)
    engine._fanout = AsyncMock()
    await engine._publish_memory_observation(moved, datetime.now(UTC))
    assert engine._fanout.mock_calls == []


async def test_provisional_observation_uses_existing_durable_outbox_and_wire_contract():
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from eidolon_memory_contracts import parse_conversation_turn

    from eidolon_agent.domain.agent.turn import TurnEngine
    from eidolon_agent.domain.history import HistoryFanout

    turn = _provisional_turn()
    outbox = AsyncMock()
    engine = object.__new__(TurnEngine)
    engine._fanout = HistoryFanout(memory_outbox=outbox)
    engine._genome_id = "g"
    await engine._publish_memory_observation(turn, datetime.now(UTC))
    outbox.enqueue.assert_awaited_once()
    entry = outbox.enqueue.call_args.kwargs
    source = parse_conversation_turn(entry["payload"])
    assert source.context.person_id == turn.context.person_id
    assert source.context.memory_realm_id == turn.context.memory_realm_id
    assert source.context.session_id == turn.session_id
    assert source.metadata["person_attribution"] == "provisional"
    assert source.assistant_text == ""
    assert turn.metadata["memory_observation_state"] == "queued"
