"""Report rendering and comparison helpers for replay artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ReplayReportComparison:
    passed: bool
    baseline_id: str
    candidate_id: str
    scenario_status_changed: list[dict[str, Any]]
    latency_regressions: list[dict[str, Any]]
    failed_checks: list[dict[str, Any]]

    def to_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": "eidolon_agent.replay_report_comparison.v1",
            "passed": self.passed,
            "baseline_id": self.baseline_id,
            "candidate_id": self.candidate_id,
            "summary": {
                "scenario_status_changed": len(self.scenario_status_changed),
                "latency_regressions": len(self.latency_regressions),
                "failed_checks": len(self.failed_checks),
            },
            "scenario_status_changed": self.scenario_status_changed,
            "latency_regressions": self.latency_regressions,
            "failed_checks": self.failed_checks,
        }


def load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("report must be a JSON object")
    return data


def render_replay_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Replay Report",
        "",
        f"- schema: `{report.get('schema_version') or 'unknown'}`",
        f"- generated_at: `{report.get('generated_at') or 'unknown'}`",
        f"- passed: `{report.get('passed')}`",
    ]
    summary = report.get("summary") or {}
    if summary:
        lines.extend(["", "## Summary"])
        for key, value in summary.items():
            lines.append(f"- {key}: `{value}`")
    metrics = report.get("metrics") or {}
    if metrics:
        lines.extend(["", "## Metrics"])
        for key, value in metrics.items():
            lines.append(f"- {key}: `{value}`")
    scenarios = report.get("scenarios") or []
    if scenarios:
        lines.extend(["", "## Scenarios"])
        for scenario in scenarios:
            status = "PASS" if scenario.get("passed") else "FAIL"
            lines.append("")
            lines.append(f"### {scenario.get('scenario_id') or 'scenario'} - {status}")
            if scenario.get("description"):
                lines.append(str(scenario["description"]))
            for turn in scenario.get("turns") or []:
                turn_label = turn.get("logical_turn_id") or turn.get("turn_id")
                lines.append(
                    f"- `{turn_label}` first_delta=`{turn.get('first_delta_ms')}` "
                    f"total=`{turn.get('total_ms')}` passed=`{turn.get('passed')}`"
                )
                for check in turn.get("checks") or []:
                    mark = (
                        "SKIP"
                        if check.get("skipped")
                        else "PASS"
                        if check.get("passed")
                        else "FAIL"
                    )
                    detail = f" ({check.get('detail')})" if check.get("detail") else ""
                    lines.append(f"  - {mark} `{check.get('name')}`{detail}")
            for check in scenario.get("checks") or []:
                mark = "SKIP" if check.get("skipped") else "PASS" if check.get("passed") else "FAIL"
                detail = f" ({check.get('detail')})" if check.get("detail") else ""
                lines.append(f"- {mark} `{check.get('name')}`{detail}")
    return "\n".join(lines) + "\n"


def render_comparison_markdown(comparison: ReplayReportComparison | dict[str, Any]) -> str:
    payload = (
        comparison.to_metadata() if isinstance(comparison, ReplayReportComparison) else comparison
    )
    summary = payload.get("summary") or {}
    lines = [
        "# Replay Report Comparison",
        "",
        f"- baseline: `{payload.get('baseline_id') or 'unknown'}`",
        f"- candidate: `{payload.get('candidate_id') or 'unknown'}`",
        f"- passed: `{payload.get('passed')}`",
        "",
        "## Summary",
        f"- scenario_status_changed: `{summary.get('scenario_status_changed', 0)}`",
        f"- latency_regressions: `{summary.get('latency_regressions', 0)}`",
        f"- failed_checks: `{summary.get('failed_checks', 0)}`",
    ]
    for section, title in (
        ("scenario_status_changed", "Scenario Status Changes"),
        ("latency_regressions", "Latency Regressions"),
        ("failed_checks", "Failed Checks"),
    ):
        items = payload.get(section) or []
        if not items:
            continue
        lines.extend(["", f"## {title}"])
        for item in items:
            scenario = item.get("scenario_id")
            turn = item.get("logical_turn_id") or item.get("turn_id")
            detail = item.get("detail") or item.get("field") or item.get("check") or ""
            delta = f" delta=`{item.get('delta_ms')}ms`" if item.get("delta_ms") is not None else ""
            lines.append(f"- scenario=`{scenario}` turn=`{turn}` `{detail}`{delta}")
    return "\n".join(lines) + "\n"


def compare_replay_reports(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_first_delta_regression_ms: int = 150,
    max_total_regression_ms: int = 250,
) -> ReplayReportComparison:
    baseline_scenarios = _scenario_map(baseline)
    candidate_scenarios = _scenario_map(candidate)
    status_changed: list[dict[str, Any]] = []
    latency_regressions: list[dict[str, Any]] = []
    failed_checks: list[dict[str, Any]] = []

    for scenario_id, candidate_scenario in candidate_scenarios.items():
        baseline_scenario = baseline_scenarios.get(scenario_id)
        if baseline_scenario is not None and bool(baseline_scenario.get("passed")) != bool(
            candidate_scenario.get("passed")
        ):
            status_changed.append(
                {
                    "scenario_id": scenario_id,
                    "baseline": bool(baseline_scenario.get("passed")),
                    "candidate": bool(candidate_scenario.get("passed")),
                }
            )
        base_turns = _turn_map(baseline_scenario or {})
        for turn in candidate_scenario.get("turns") or []:
            turn_key = _turn_key(turn)
            base_turn = base_turns.get(turn_key)
            if base_turn is not None:
                for field, threshold in (
                    ("first_delta_ms", max_first_delta_regression_ms),
                    ("total_ms", max_total_regression_ms),
                ):
                    base_val = _int_or_none(base_turn.get(field))
                    cand_val = _int_or_none(turn.get(field))
                    if (
                        base_val is not None
                        and cand_val is not None
                        and cand_val - base_val > threshold
                    ):
                        latency_regressions.append(
                            {
                                "scenario_id": scenario_id,
                                "turn_id": turn.get("turn_id"),
                                "logical_turn_id": turn_key,
                                "field": field,
                                "baseline": base_val,
                                "candidate": cand_val,
                                "delta_ms": cand_val - base_val,
                                "threshold_ms": threshold,
                            }
                        )
            for check in turn.get("checks") or []:
                if check.get("passed") is False and not check.get("skipped"):
                    failed_checks.append(
                        {
                            "scenario_id": scenario_id,
                            "turn_id": turn.get("turn_id"),
                            "logical_turn_id": turn_key,
                            "check": check.get("name"),
                            "detail": check.get("detail"),
                        }
                    )

    return ReplayReportComparison(
        passed=not status_changed and not latency_regressions and not failed_checks,
        baseline_id=str(
            baseline.get("generated_at") or baseline.get("schema_version") or "baseline"
        ),
        candidate_id=str(
            candidate.get("generated_at") or candidate.get("schema_version") or "candidate"
        ),
        scenario_status_changed=status_changed,
        latency_regressions=latency_regressions,
        failed_checks=failed_checks,
    )


def _scenario_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(s.get("scenario_id")): s
        for s in report.get("scenarios") or []
        if isinstance(s, dict) and s.get("scenario_id")
    }


def _turn_map(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _turn_key(t): t
        for t in scenario.get("turns") or []
        if isinstance(t, dict) and (t.get("logical_turn_id") or t.get("turn_id"))
    }


def _turn_key(turn: dict[str, Any]) -> str:
    return str(turn.get("logical_turn_id") or turn.get("turn_id"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ReplayReportComparison",
    "compare_replay_reports",
    "load_report",
    "render_comparison_markdown",
    "render_replay_markdown",
]
