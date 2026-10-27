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
