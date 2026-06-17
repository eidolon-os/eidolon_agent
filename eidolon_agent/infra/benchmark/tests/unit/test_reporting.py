from __future__ import annotations

import json
from datetime import datetime, timezone

from eidolon_agent.infra.benchmark.reporting import (
    SCHEMA_VERSION,
    build_realtime_benchmark_report,
    normalize_flat_turns,
    write_benchmark_artifacts,
)


def test_build_realtime_report_exposes_stable_admin_contract() -> None:
    scenarios, turns = normalize_flat_turns(
        [
            {
                "turn_id": "t1",
                "first_delta_ms": 12,
                "total_ms": 40,
                "passed": True,
                "checks": [{"name": "stream_done", "passed": True}],
            },
            {
                "turn_id": "t2",
                "first_delta_ms": 20,
                "total_ms": 60,
                "passed": True,
                "checks": [{"name": "stream_done", "passed": True}],
            },
        ],
        mode="hotpath",
        scenario_id="hotpath",
        description="framework latency",
    )

    report = build_realtime_benchmark_report(
        run_id="unit",
        mode="hotpath",
        profile="voice",
        target={"mode": "hotpath"},
        thresholds={"first_delta_p95_ms": 50, "total_p95_ms": 100},
        scenarios=scenarios,
        turns=turns,
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert report["schema_version"] == SCHEMA_VERSION
    assert report["kind"] == "realtime_benchmark"
    assert report["passed"] is True
    assert report["summary"] == {
        "scenario_count": 1,
        "turn_count": 2,
        "passed_turn_count": 2,
        "failed_turn_count": 0,
        "failed_check_count": 0,
        "threshold_failed_count": 0,
    }
    assert report["metrics"]["first_delta_ms"]["p95"] == 20
    assert report["metrics"]["total_ms"]["p50"] == 40
    assert report["visualization"]["primary_latency_fields"] == [
        "first_delta_ms",
        "total_ms",
    ]


def test_threshold_and_baseline_regressions_fail_report() -> None:
    scenarios, turns = normalize_flat_turns(
        [
            {
                "turn_id": "slow",
                "first_delta_ms": 1000,
                "total_ms": 3000,
                "passed": True,
            }
        ],
        mode="hotpath",
        scenario_id="hotpath",
        description="slow",
    )
    baseline = {
        "schema_version": SCHEMA_VERSION,
        "run_id": "base",
        "metrics": {
            "first_delta_ms": {"p50": 100, "p95": 100, "p99": 100},
            "total_ms": {"p50": 400, "p95": 400, "p99": 400},
        },
    }

    report = build_realtime_benchmark_report(
        run_id="current",
        mode="hotpath",
        profile="voice",
        target={},
        thresholds={
            "first_delta_p95_ms": 200,
            "total_p95_ms": 1000,
            "baseline_max_regression_ms": 50,
            "baseline_max_regression_ratio": 0.1,
        },
        scenarios=scenarios,
        turns=turns,
        baseline_report=baseline,
    )

    assert report["passed"] is False
    assert report["summary"]["threshold_failed_count"] == 2
    assert report["baseline"]["regressed"] is True


def test_write_benchmark_artifacts_includes_human_reports_and_latest(tmp_path) -> None:
    scenarios, turns = normalize_flat_turns(
        [{"turn_id": "t1", "first_delta_ms": 10, "total_ms": 20, "passed": True}],
        mode="hotpath",
        scenario_id="hotpath",
        description="framework latency",
    )
    report = build_realtime_benchmark_report(
        run_id="artifact",
        mode="hotpath",
        profile="voice",
        target={},
        thresholds={},
        scenarios=scenarios,
        turns=turns,
    )

    artifacts = write_benchmark_artifacts(
        report,
        output_json=tmp_path / "benchmark-artifact.json",
    )

    payload = json.loads((tmp_path / "benchmark-artifact.json").read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert (tmp_path / "benchmark-artifact.md").read_text().startswith(
        "# Realtime Benchmark"
    )
    assert "<!doctype html>" in (tmp_path / "benchmark-artifact.html").read_text()
    assert json.loads((tmp_path / "latest.json").read_text())["run_id"] == "artifact"
    assert json.loads((tmp_path / "latest-hotpath.json").read_text())["run_id"] == "artifact"
    assert artifacts["json"].endswith("benchmark-artifact.json")
