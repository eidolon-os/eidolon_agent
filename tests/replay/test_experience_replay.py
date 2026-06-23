"""Experience Replay Suite — product-level companion behavior checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eidolon_agent.app.replay import load_replay_scenarios, run_replay_files

pytestmark = pytest.mark.integration


async def test_core_experience_replay_fixture_passes() -> None:
    report = await run_replay_files([Path("tests/replay/fixtures/core_experience.jsonl")])

    assert report["passed"] is True
    assert report["summary"]["scenario_count"] == 6
    by_id = {s["scenario_id"]: s for s in report["scenarios"]}
    assert by_id["forget-privacy"]["passed"] is True
    assert by_id["memory-backend-down"]["passed"] is True


async def test_topic_switch_context_replay_fixture_passes() -> None:
    report = await run_replay_files(
        [Path("tests/replay/fixtures/topic_switch_context.jsonl")]
    )

    assert report["passed"] is True
    assert report["summary"]["scenario_count"] == 4
    by_id = {s["scenario_id"]: s for s in report["scenarios"]}
    assert by_id["weather_topic_switch_counting"]["passed"] is True
    assert by_id["weather_failure_correction"]["passed"] is True
    assert by_id["interrupted_tool_then_new_request"]["passed"] is True
    assert by_id["multi_turn_reference_without_reexecution"]["passed"] is True


async def test_experience_replay_attaches_memory_quality_summary(tmp_path: Path) -> None:
    memory_report = tmp_path / "memory_quality.json"
    memory_report.write_text(
        json.dumps({"summary": {"passed": True, "precision": 0.91}}),
        encoding="utf-8",
    )

    report = await run_replay_files(
        [Path("tests/replay/fixtures/core_experience.jsonl")],
        memory_report_path=memory_report,
    )

    assert report["memory_quality"] == {"passed": True, "precision": 0.91}


def test_replay_fixture_loader_rejects_bad_json(tmp_path: Path) -> None:
    fixture = tmp_path / "bad.jsonl"
    fixture.write_text("{bad", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid JSON"):
        load_replay_scenarios([fixture])
