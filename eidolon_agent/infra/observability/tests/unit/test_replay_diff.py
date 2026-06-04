"""Replay diff rollout gate."""

from __future__ import annotations

import pytest

from eidolon_agent.infra.observability.replay_diff import (
    ReplayDiffThresholds,
    ReplayTurnSnapshot,
    compare_replay_snapshots,
    snapshots_from_artifact,
)

pytestmark = pytest.mark.unit


def test_gate_passes_when_prompt_tool_and_latency_are_stable() -> None:
    baseline = [_snap("t1", prompt="p", total=100, first=40)]
    candidate = [_snap("t1-new", prompt="p", total=120, first=50)]

    report = compare_replay_snapshots(baseline, candidate)

    assert report.passed is True
    assert report.to_metadata()["summary"]["latency_regressions"] == 0


def test_gate_blocks_prompt_shape_drift_over_threshold() -> None:
    report = compare_replay_snapshots(
        [_snap("t1", prompt="old")],
        [_snap("t1", prompt="new")],
        thresholds=ReplayDiffThresholds(max_prompt_change_ratio=0.0),
    )

    assert report.passed is False
    assert report.prompt_changed[0]["baseline"] == "old"


def test_gate_blocks_tool_decision_drift() -> None:
    report = compare_replay_snapshots(
        [_snap("t1", tools=("get_time:True:",))],
        [_snap("t1", tools=("emit_event:False:eidolon.tool_permission_denied",))],
    )

    assert report.passed is False
    assert report.tool_decision_changed[0]["candidate"] == [
        "emit_event:False:eidolon.tool_permission_denied"
    ]


def test_gate_blocks_latency_regression_only_when_absolute_and_ratio_exceed() -> None:
    report = compare_replay_snapshots(
        [_snap("t1", total=200, first=80)],
        [_snap("t1", total=420, first=260)],
        thresholds=ReplayDiffThresholds(
            max_latency_regression_ms=100,
            max_latency_regression_ratio=0.25,
        ),
    )

    assert report.passed is False
    assert {r.field for r in report.latency_regressions} == {"first_delta_ms", "total_ms"}


def test_gate_blocks_missing_or_extra_turns() -> None:
    report = compare_replay_snapshots([_snap("t1"), _snap("t2")], [_snap("t1")])

    assert report.passed is False
    assert report.missing_turns == 1


def test_snapshots_from_turn_trace_artifact_are_prompt_safe() -> None:
    artifact = {
        "turns": [
            {
                "metadata": {
                    "turn_trace": {
                        "schema_version": "turn_trace.v1",
                        "turn": {
                            "turn_id": "t1",
                            "trigger": "user_utterance",
                            "triage": "simple",
                        },
                        "latency": {"first_delta_ms": 50, "total_ms": 180},
                        "context_ledger": {
                            "segments": [
                                {
                                    "kind": "persona",
                                    "source": "personas_service",
                                    "token_estimate": 143,
                                },
                                {
                                    "kind": "memory",
                                    "source": "memory",
                                    "token_estimate": 20,
                                    "metadata": {"degraded": False},
                                },
                            ],
                            "degraded_sources": [],
                        },
                        "memory_trace": {
                            "attempted": True,
                            "degraded": False,
                            "hit_count": 1,
                            "context_injected": True,
                        },
                        "tool_trace": [
                            {
                                "name": "get_time",
                                "ok": True,
                                "error_code": None,
                            }
                        ],
                        "privacy": {"mode": "normal"},
                    }
                }
            }
        ]
    }

    snapshots = snapshots_from_artifact(artifact)

    assert snapshots[0].key == "t1"
    assert snapshots[0].tool_decision == ("get_time:True:",)
    assert snapshots[0].first_delta_ms == 50
    assert "secret" not in snapshots[0].prompt_fingerprint


def _snap(
    key: str,
    *,
    prompt: str = "p",
    tools: tuple[str, ...] = (),
    first: int | None = None,
    total: int | None = None,
) -> ReplayTurnSnapshot:
    return ReplayTurnSnapshot(
        key=key,
        prompt_fingerprint=prompt,
        tool_decision=tools,
        first_delta_ms=first,
        total_ms=total,
    )
