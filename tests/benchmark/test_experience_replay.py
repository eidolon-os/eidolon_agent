"""Experience Replay Suite — product-level companion behavior checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eidolon_agent.app.benchmark import load_replay_scenarios, run_replay_files
from eidolon_agent.app.benchmark.experience import run_replay_scenarios
from eidolon_agent.app.benchmark.suites import agent_memory_experience_scenarios

pytestmark = pytest.mark.integration


async def test_core_experience_replay_fixture_passes() -> None:
    report = await run_replay_files([Path("tests/benchmark/fixtures/core_experience.jsonl")])

    assert report["passed"] is True
    assert report["summary"]["scenario_count"] == 6
    by_id = {s["scenario_id"]: s for s in report["scenarios"]}
    assert by_id["forget-privacy"]["passed"] is True
    assert by_id["memory-backend-down"]["passed"] is True


async def test_topic_switch_context_replay_fixture_passes() -> None:
    report = await run_replay_files(
        [Path("tests/benchmark/fixtures/topic_switch_context.jsonl")]
    )

    assert report["passed"] is True
    assert report["summary"]["scenario_count"] == 4
    by_id = {s["scenario_id"]: s for s in report["scenarios"]}
    assert by_id["weather_topic_switch_counting"]["passed"] is True
    assert by_id["weather_failure_correction"]["passed"] is True
    assert by_id["interrupted_tool_then_new_request"]["passed"] is True
    assert by_id["multi_turn_reference_without_reexecution"]["passed"] is True


async def test_agent_memory_experience_benchmark_is_broad_and_passes() -> None:
    scenarios = agent_memory_experience_scenarios()

    assert len(scenarios) >= 100
    assert {scenario["category"] for scenario in scenarios} >= {
        "context_authority",
        "memory_use",
        "memory_update",
        "memory_abstention",
        "memory_privacy",
        "agent_tool_control",
        "interrupt_realtime",
        "multi_turn_reference",
    }

    report = await run_replay_scenarios(scenarios)

    assert report["passed"] is True
    assert report["summary"]["scenario_count"] == len(scenarios)
    assert report["metrics"]["turn_count"] >= 180
    assert report["metrics"]["check_pass_rate"] == 1.0
    assert report["metrics"]["categories"]["context_authority"]["scenario_count"] == 24


async def test_experience_replay_attaches_memory_quality_summary(tmp_path: Path) -> None:
    memory_report = tmp_path / "memory_quality.json"
    memory_report.write_text(
        json.dumps({"summary": {"passed": True, "precision": 0.91}}),
        encoding="utf-8",
    )

    report = await run_replay_files(
        [Path("tests/benchmark/fixtures/core_experience.jsonl")],
        memory_report_path=memory_report,
    )

    assert report["memory_quality"] == {"passed": True, "precision": 0.91}


def test_replay_fixture_loader_rejects_bad_json(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.jsonl"
    fixture.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        load_replay_scenarios([fixture])
