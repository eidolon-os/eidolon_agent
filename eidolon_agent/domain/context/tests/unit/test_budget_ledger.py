"""ContextBudget / ContextLedger pure behavior."""

from __future__ import annotations

import pytest

from eidolon_agent.domain.context.types import (
    ContextBudget,
    ContextLedger,
    ContextSegment,
    ContextSegmentKind,
)

pytestmark = pytest.mark.unit


def _seg(kind: ContextSegmentKind, tokens: int, *, priority: int = 50) -> ContextSegment:
    return ContextSegment(
        kind=kind,
        content=f"{kind.value}-content",
        source=kind.value,
        token_estimate=tokens,
        priority=priority,
    )


def test_persona_and_current_user_are_kept_even_over_budget() -> None:
    persona = _seg(ContextSegmentKind.PERSONA, 80)
    policy = _seg(ContextSegmentKind.HARNESS_POLICY, 60)
    memory = _seg(ContextSegmentKind.MEMORY, 30)
    current = _seg(ContextSegmentKind.CURRENT_USER, 80)

    kept, ledger = ContextBudget(max_tokens=50).prune([persona, policy, memory, current])

    assert [s.kind for s in kept] == [
        ContextSegmentKind.PERSONA,
        ContextSegmentKind.HARNESS_POLICY,
        ContextSegmentKind.CURRENT_USER,
    ]
    assert ledger.total_token_estimate == 220
    assert ledger.dropped_segments[0].kind is ContextSegmentKind.MEMORY
    assert ledger.dropped_segments[0].reason == "token_budget_exceeded"


def test_optional_segments_are_pruned_by_priority() -> None:
    persona = _seg(ContextSegmentKind.PERSONA, 10)
    policy = _seg(ContextSegmentKind.HARNESS_POLICY, 10)
    memory = _seg(ContextSegmentKind.MEMORY, 30, priority=90)
    history = _seg(ContextSegmentKind.HISTORY, 30, priority=20)
    summary = _seg(ContextSegmentKind.SUMMARY, 20, priority=70)
    current = _seg(ContextSegmentKind.CURRENT_USER, 10)

    kept, ledger = ContextBudget(max_tokens=80).prune(
        [persona, policy, history, memory, summary, current]
    )

    assert [s.kind for s in kept] == [
        ContextSegmentKind.PERSONA,
        ContextSegmentKind.HARNESS_POLICY,
        ContextSegmentKind.MEMORY,
        ContextSegmentKind.SUMMARY,
        ContextSegmentKind.CURRENT_USER,
    ]
    assert [d.kind for d in ledger.dropped_segments] == [ContextSegmentKind.HISTORY]


def test_ledger_metadata_is_prompt_safe_and_tracks_degraded_sources() -> None:
    ledger = ContextLedger(
        kept_segments=[
            ContextSegment(
                kind=ContextSegmentKind.MEMORY,
                content="full recalled private-ish text",
                source="memory",
                token_estimate=12,
                metadata={"hit_ids": ["m1"]},
            )
        ],
    )
    ledger.mark_degraded("memory")
    meta = ledger.to_metadata()

    assert meta["degraded_sources"] == ["memory"]
    assert meta["segments"][0]["metadata"] == {"hit_ids": ["m1"]}
    assert "full recalled private-ish text" not in str(meta)
