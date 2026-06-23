from __future__ import annotations

from eidolon_agent.app.replay import (
    compare_replay_reports,
    render_comparison_markdown,
    render_replay_markdown,
)


def test_render_replay_markdown_uses_logical_turn_id_and_skips() -> None:
    report = {
        "schema_version": "x",
        "generated_at": "2026-06-05T00:00:00Z",
        "passed": True,
        "summary": {"scenario_count": 1},
        "scenarios": [
            {
                "scenario_id": "s1",
                "passed": True,
                "turns": [
                    {
                        "turn_id": "actual-random",
                        "logical_turn_id": "stable-1",
                        "passed": True,
                        "first_delta_ms": 10,
                        "total_ms": 20,
                        "checks": [
                            {
                                "name": "operator_note",
                                "passed": True,
                                "skipped": True,
                                "detail": "manual only",
                            }
                        ],
                    }
                ],
            }
        ],
    }

    out = render_replay_markdown(report)

    assert "`stable-1` first_delta=`10`" in out
    assert "SKIP `operator_note`" in out


def test_compare_replay_reports_uses_logical_turn_id_for_live_runs() -> None:
    baseline = {
        "generated_at": "baseline",
        "scenarios": [
            {
                "scenario_id": "s1",
                "passed": True,
                "turns": [
                    {
                        "turn_id": "stable-1-old",
                        "logical_turn_id": "stable-1",
                        "first_delta_ms": 100,
                        "total_ms": 200,
                        "checks": [{"name": "ok", "passed": True}],
                    }
                ],
            }
        ],
    }
    candidate = {
        "generated_at": "candidate",
        "scenarios": [
            {
                "scenario_id": "s1",
                "passed": True,
                "turns": [
                    {
                        "turn_id": "stable-1-new",
                        "logical_turn_id": "stable-1",
                        "first_delta_ms": 260,
                        "total_ms": 390,
                        "checks": [{"name": "manual", "passed": False, "skipped": True}],
                    }
                ],
            }
        ],
    }

    comparison = compare_replay_reports(
        baseline,
        candidate,
        max_first_delta_regression_ms=150,
        max_total_regression_ms=250,
    )

    assert comparison.passed is False
    assert comparison.failed_checks == []
    assert comparison.latency_regressions[0]["logical_turn_id"] == "stable-1"
    assert comparison.latency_regressions[0]["delta_ms"] == 160


def test_render_comparison_markdown_is_operator_readable() -> None:
    comparison = compare_replay_reports(
        {
            "generated_at": "b",
            "scenarios": [
                {
                    "scenario_id": "s",
                    "passed": True,
                    "turns": [{"turn_id": "t", "first_delta_ms": 10}],
                }
            ],
        },
        {
            "generated_at": "c",
            "scenarios": [
                {
                    "scenario_id": "s",
                    "passed": False,
                    "turns": [
                        {
                            "turn_id": "t",
                            "first_delta_ms": 200,
                            "checks": [{"name": "required_assistant", "passed": False}],
                        }
                    ],
                }
            ],
        },
    )

    out = render_comparison_markdown(comparison)

    assert "# Replay Report Comparison" in out
    assert "Scenario Status Changes" in out
    assert "Latency Regressions" in out
    assert "Failed Checks" in out
