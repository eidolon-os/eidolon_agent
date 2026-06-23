"""Report rendering and comparison helpers for replay artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
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


def render_replay_html(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    metrics = report.get("metrics") or {}
    categories = (metrics.get("categories") or {}) if isinstance(metrics, dict) else {}
    scenarios = report.get("scenarios") or []
    failed = [
        scenario
        for scenario in scenarios
        if isinstance(scenario, dict) and not bool(scenario.get("passed"))
    ]
    category_rows = []
    for name, bucket in sorted(categories.items()):
        category_rows.append(
            "<tr>"
            f"<td>{escape(str(name))}</td>"
            f"<td>{bucket.get('scenario_count', 0)}</td>"
            f"<td>{bucket.get('turn_count', 0)}</td>"
            f"<td>{bucket.get('passed', 0)}</td>"
            f"<td>{bucket.get('failed', 0)}</td>"
            "</tr>"
        )
    scenario_rows = []
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        status = "pass" if scenario.get("passed") else "fail"
        checks_failed = _failed_check_count(scenario)
        scenario_rows.append(
            f"<tr class='{status}'>"
            f"<td>{escape(str(scenario.get('scenario_id') or ''))}</td>"
            f"<td>{escape(str(scenario.get('category') or ''))}</td>"
            f"<td>{len(scenario.get('turns') or [])}</td>"
            f"<td>{checks_failed}</td>"
            f"<td>{escape(str(scenario.get('description') or ''))}</td>"
            "</tr>"
        )
    first_delta = metrics.get("first_delta_ms") or {}
    total = metrics.get("total_ms") or {}
    return (
        "<!doctype html>\n"
        "<html><head><meta charset='utf-8'>"
        "<title>Eidolon Replay Report</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#202124;background:#f7f8fa}"
        "h1,h2{margin:0 0 12px}section{margin:0 0 24px}table{border-collapse:collapse;width:100%;background:white}"
        "th,td{border:1px solid #dde1e6;padding:8px;text-align:left;vertical-align:top}th{background:#eef2f7}"
        ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}.card{background:white;border:1px solid #dde1e6;padding:12px}"
        ".value{font-size:24px;font-weight:650}.pass td:first-child{border-left:4px solid #1f8f4d}.fail td:first-child{border-left:4px solid #c83f31}"
        ".ok{color:#1f8f4d}.bad{color:#c83f31}"
        "</style></head><body>"
        "<h1>Eidolon Replay Report</h1>"
        f"<p>generated_at: <code>{escape(str(report.get('generated_at') or 'unknown'))}</code></p>"
        "<section class='cards'>"
        f"{_metric_card('Passed', report.get('passed'))}"
        f"{_metric_card('Scenarios', summary.get('scenario_count'))}"
        f"{_metric_card('Turns', metrics.get('turn_count'))}"
        f"{_metric_card('Checks', metrics.get('check_count'))}"
        f"{_metric_card('Check Pass Rate', metrics.get('check_pass_rate'))}"
        f"{_metric_card('TTFT p95', first_delta.get('p95'))}"
        f"{_metric_card('Total p95', total.get('p95'))}"
        f"{_metric_card('Failed Scenarios', len(failed))}"
        "</section>"
        "<section><h2>Categories</h2><table><thead><tr>"
        "<th>Category</th><th>Scenarios</th><th>Turns</th><th>Passed</th><th>Failed</th>"
        "</tr></thead><tbody>"
        + "".join(category_rows)
        + "</tbody></table></section>"
        "<section><h2>Scenarios</h2><table><thead><tr>"
        "<th>Scenario</th><th>Category</th><th>Turns</th><th>Failed Checks</th><th>Description</th>"
        "</tr></thead><tbody>"
        + "".join(scenario_rows)
        + "</tbody></table></section>"
        "</body></html>\n"
    )


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


def _failed_check_count(scenario: dict[str, Any]) -> int:
    count = 0
    for turn in scenario.get("turns") or []:
        for check in (turn or {}).get("checks") or []:
            if check.get("passed") is False and not check.get("skipped"):
                count += 1
    for check in scenario.get("checks") or []:
        if check.get("passed") is False and not check.get("skipped"):
            count += 1
    return count


def _metric_card(label: str, value: Any) -> str:
    klass = ""
    if label == "Passed":
        klass = " ok" if bool(value) else " bad"
    return (
        "<div class='card'>"
        f"<div>{escape(label)}</div>"
        f"<div class='value{klass}'>{escape(str(value))}</div>"
        "</div>"
    )


__all__ = [
    "ReplayReportComparison",
    "compare_replay_reports",
    "load_report",
    "render_comparison_markdown",
    "render_replay_html",
    "render_replay_markdown",
]
