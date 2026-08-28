from __future__ import annotations

import json
from datetime import datetime, timezone

from eidolon_agent.infra.benchmark.reporting import (
    SCHEMA_VERSION,
    build_realtime_benchmark_report,
    normalize_flat_turns,
    normalize_live_service_report,
    write_benchmark_artifacts,
    write_standard_benchmark_run,
)


def test_live_service_normalization_preserves_activity_visible_latency_matrix() -> None:
    scenarios, turns = normalize_live_service_report(
        {
            "scenarios": [
                {
                    "scenario_id": "reasoning",
                    "passed": True,
                    "turns": [
                        {
                            "turn_id": "t-reasoning",
                            "logical_turn_id": "repeat-1",
                            "passed": True,
                            "first_progress_ms": 240,
                            "first_model_activity_ms": 240,
                            "first_delta_ms": 12_400,
                            "total_ms": 13_000,
                            "output_path": "visible_after_progress",
                        }
                    ],
                }
            ]
        },
        mode="live-service",
    )

    assert turns[0]["first_progress_ms"] == 240
    assert turns[0]["first_delta_ms"] == 12_400
    assert turns[0]["output_path"] == "visible_after_progress"
    assert scenarios[0]["turns"][0]["first_model_activity_ms"] == 240


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
    assert report["diagnosis"]["status"] == "failed"
    assert any(
        "首响延迟" in item["summary"] for item in report["diagnosis"]["top_causes"]
    )


def test_diagnosis_groups_failure_causes_and_scenarios(tmp_path) -> None:
    scenarios, turns = normalize_flat_turns(
        [
            {
                "turn_id": "slow",
                "first_delta_ms": 3200,
                "total_ms": 4100,
                "passed": False,
                "checks": [{"name": "max_first_delta_ms", "passed": False, "detail": "got=3200"}],
            },
            {
                "turn_id": "tool",
                "first_delta_ms": 100,
                "total_ms": 300,
                "passed": False,
                "checks": [{"name": "tool_name:emit_event", "passed": False, "detail": "got=[]"}],
            },
        ],
        mode="live-service",
        scenario_id="live-tool-permission-denied",
        description="real service tool policy",
    )
    report = build_realtime_benchmark_report(
        run_id="diagnosis",
        mode="live-service",
        profile="voice",
        target={},
        thresholds={"first_delta_p95_ms": 3000},
        scenarios=scenarios,
        turns=turns,
    )

    diagnosis = report["diagnosis"]
    assert diagnosis["status"] == "failed"
    assert diagnosis["counts"]["failed_turns"] == 2
    assert {item["category"] for item in diagnosis["top_causes"]} >= {
        "latency_first_delta",
        "tool_behavior",
    }
    assert diagnosis["scenario_breakdown"][0]["scenario_id"] == "live-tool-permission-denied"
    assert diagnosis["recommendations"]
    artifacts = write_benchmark_artifacts(
        report,
        output_json=tmp_path / "diagnosis.json",
        write_latest=False,
    )
    payload = json.loads((tmp_path / "diagnosis.json").read_text())
    markdown = (tmp_path / "diagnosis.md").read_text()
    assert payload["diagnosis"]["headline"]
    assert "## Diagnosis" in markdown
    assert "### Top Causes" in markdown
    assert artifacts["json"].endswith("diagnosis.json")


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
    report["llm_summary"] = {
        "status": "ok",
        "model_id": "test-model",
        "text": "总体看，首响稳定；暂无不达标项。",
    }

    artifacts = write_benchmark_artifacts(
        report,
        output_json=tmp_path / "benchmark-artifact.json",
    )

    payload = json.loads((tmp_path / "benchmark-artifact.json").read_text())
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["llm_summary"]["status"] == "ok"
    assert (tmp_path / "benchmark-artifact.md").read_text().startswith(
        "# Realtime Benchmark"
    )
    assert "## LLM Summary" in (tmp_path / "benchmark-artifact.md").read_text()
    assert "<!doctype html>" in (tmp_path / "benchmark-artifact.html").read_text()
    assert "LLM Summary" in (tmp_path / "benchmark-artifact.html").read_text()
    assert json.loads((tmp_path / "latest.json").read_text())["run_id"] == "artifact"
    assert json.loads((tmp_path / "latest-hotpath.json").read_text())["run_id"] == "artifact"
    assert artifacts["json"].endswith("benchmark-artifact.json")


def test_write_standard_benchmark_run_uses_admin_directory_contract(tmp_path) -> None:
    scenarios, turns = normalize_flat_turns(
        [{"turn_id": "t1", "first_delta_ms": 10, "total_ms": 20, "passed": True}],
        mode="live-service",
        scenario_id="live-service-smoke",
        description="real service smoke",
    )
    report = build_realtime_benchmark_report(
        run_id="live-service-20260623T120000Z-a1b2c3",
        mode="live-service",
        profile="voice",
        target={"http_base": "http://127.0.0.1:8081"},
        thresholds={},
        scenarios=scenarios,
        turns=turns,
    )

    artifacts = write_standard_benchmark_run(
        report,
        runs_dir=tmp_path,
        suite="realtime-live-service",
        git_sha="abc123",
    )

    run_dir = tmp_path / "realtime-live-service" / report["run_id"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    full_report = json.loads((run_dir / "report.json").read_text())
    assert artifacts["run_dir"] == str(run_dir)
    assert manifest["schema_version"] == "eidolon_agent.benchmark_run_manifest.v1"
    assert manifest["run"]["git_sha"] == "abc123"
    assert manifest["summary"]["turn_count"] == 1
    assert manifest["diagnosis"]["status"] == "passed"
    assert manifest["metrics"]["total_ms"]["p95"] == 20
    assert manifest["cases"][0]["scenario_id"] == "live-service-smoke"
    assert full_report["schema_version"] == SCHEMA_VERSION
    assert (run_dir / "report.md").read_text().startswith("# Realtime Benchmark")
    assert "<!doctype html>" in (run_dir / "report.html").read_text()
