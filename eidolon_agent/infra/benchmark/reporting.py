"""Machine-readable and human-readable realtime benchmark reports."""

from __future__ import annotations

import html
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "eidolon_agent.realtime_benchmark_report.v1"

_LATENCY_FIELDS = ("first_delta_ms", "total_ms")


def build_realtime_benchmark_report(
    *,
    run_id: str,
    mode: str,
    profile: str,
    target: dict[str, Any],
    thresholds: dict[str, Any],
    scenarios: list[dict[str, Any]],
    turns: list[dict[str, Any]],
    source_reports: list[dict[str, Any]] | None = None,
    baseline_report: dict[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(timezone.utc)
    metrics = {
        field: _latency_stats([turn.get(field) for turn in turns])
        for field in _LATENCY_FIELDS
    }
    failed_turns = [turn for turn in turns if not turn.get("passed", True)]
    failed_checks = [
        {
            "scenario_id": turn.get("scenario_id"),
            "turn_id": turn.get("turn_id"),
            "name": check.get("name"),
            "detail": check.get("detail"),
        }
        for turn in turns
        for check in turn.get("checks", [])
        if not check.get("passed", True)
    ]
    threshold_checks = _threshold_checks(metrics, thresholds)
    passed = (
        not failed_turns
        and not failed_checks
        and all(check["passed"] for check in threshold_checks)
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "run_id": run_id,
        "kind": "realtime_benchmark",
        "mode": mode,
        "profile": profile,
        "passed": passed,
        "target": dict(target),
        "thresholds": dict(thresholds),
        "summary": {
            "scenario_count": len(scenarios),
            "turn_count": len(turns),
            "passed_turn_count": len(turns) - len(failed_turns),
            "failed_turn_count": len(failed_turns),
            "failed_check_count": len(failed_checks),
            "threshold_failed_count": sum(1 for c in threshold_checks if not c["passed"]),
        },
        "metrics": metrics,
        "threshold_checks": threshold_checks,
        "dimensions": _dimension_metrics(turns),
        "scenarios": scenarios,
        "turns": turns,
        "failed_checks": failed_checks,
        "visualization": {
            "primary_latency_fields": list(_LATENCY_FIELDS),
            "recommended_charts": [
                {
                    "id": "latency_distribution",
                    "type": "percentile_cards",
                    "metrics": ["first_delta_ms", "total_ms"],
                },
                {
                    "id": "turn_latency_bars",
                    "type": "bar",
                    "x": "turns[].logical_turn_id",
                    "y": ["turns[].first_delta_ms", "turns[].total_ms"],
                },
                {
                    "id": "scenario_pass_rate",
                    "type": "table",
                    "source": "scenarios[]",
                },
            ],
        },
    }
    if source_reports:
        report["source_reports"] = source_reports
    if baseline_report is not None:
        report["baseline"] = compare_to_baseline(
            report,
            baseline_report=baseline_report,
            max_regression_ms=int(thresholds.get("baseline_max_regression_ms", 150)),
            max_regression_ratio=float(
                thresholds.get("baseline_max_regression_ratio", 0.25)
            ),
        )
        if report["baseline"]["regressed"]:
            report["passed"] = False
    return report


def normalize_experience_report(report: dict[str, Any], *, mode: str) -> tuple[list[dict], list[dict]]:
    scenarios: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    for scenario in report.get("scenarios") or []:
        scenario_turns = []
        for turn in scenario.get("turns") or []:
            normalized = _normalize_turn(
                mode=mode,
                scenario_id=scenario.get("scenario_id"),
                turn=turn,
                trace_summary=turn.get("trace_summary"),
            )
            turns.append(normalized)
            scenario_turns.append(
                {
                    "turn_id": normalized["turn_id"],
                    "passed": normalized["passed"],
                    "first_delta_ms": normalized["first_delta_ms"],
                    "total_ms": normalized["total_ms"],
                }
            )
        scenarios.append(
            {
                "scenario_id": scenario.get("scenario_id"),
                "description": scenario.get("description", ""),
                "passed": bool(scenario.get("passed")),
                "turn_count": len(scenario_turns),
                "turns": scenario_turns,
                "checks": scenario.get("checks") or [],
            }
        )
    return scenarios, turns


def normalize_live_service_report(report: dict[str, Any], *, mode: str) -> tuple[list[dict], list[dict]]:
    scenarios: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    for scenario in report.get("scenarios") or []:
        scenario_turns = []
        for turn in scenario.get("turns") or []:
            admin = turn.get("admin") or {}
            obs = admin.get("observability_summary") or {}
            trace_summary = {
                "privacy": obs.get("privacy_mode"),
                "context": {
                    "segments": (obs.get("context") or {}).get("segment_kinds") or [],
                    "dropped": (obs.get("context") or {}).get("dropped_kinds") or [],
                    "degraded_sources": (obs.get("context") or {}).get(
                        "degraded_sources"
                    )
                    or [],
                },
                "memory_recall": obs.get("memory") or {},
                "memory_write": obs.get("memory_write") or {},
                "tools": obs.get("tools") or {},
            }
            normalized = _normalize_turn(
                mode=mode,
                scenario_id=scenario.get("scenario_id"),
                turn=turn,
                trace_summary=trace_summary,
            )
            normalized["event_kinds"] = list(turn.get("event_kinds") or [])
            normalized["assistant_preview"] = turn.get("assistant_preview", "")
            turns.append(normalized)
            scenario_turns.append(
                {
                    "turn_id": normalized["turn_id"],
                    "logical_turn_id": normalized["logical_turn_id"],
                    "passed": normalized["passed"],
                    "first_delta_ms": normalized["first_delta_ms"],
                    "total_ms": normalized["total_ms"],
                }
            )
        scenarios.append(
            {
                "scenario_id": scenario.get("scenario_id"),
                "description": scenario.get("description", ""),
                "passed": bool(scenario.get("passed")),
                "turn_count": len(scenario_turns),
                "turns": scenario_turns,
                "checks": scenario.get("checks") or [],
            }
        )
    return scenarios, turns


def normalize_flat_turns(
    turns: list[dict[str, Any]],
    *,
    mode: str,
    scenario_id: str,
    description: str,
) -> tuple[list[dict], list[dict]]:
    normalized = [
        _normalize_turn(mode=mode, scenario_id=scenario_id, turn=turn, trace_summary={})
        for turn in turns
    ]
    scenario = {
        "scenario_id": scenario_id,
        "description": description,
        "passed": all(turn.get("passed", True) for turn in normalized),
        "turn_count": len(normalized),
        "turns": [
            {
                "turn_id": turn["turn_id"],
                "logical_turn_id": turn["logical_turn_id"],
                "passed": turn["passed"],
                "first_delta_ms": turn["first_delta_ms"],
                "total_ms": turn["total_ms"],
            }
            for turn in normalized
        ],
        "checks": [],
    }
    return [scenario], normalized


def compare_to_baseline(
    report: dict[str, Any],
    *,
    baseline_report: dict[str, Any],
    max_regression_ms: int,
    max_regression_ratio: float,
) -> dict[str, Any]:
    comparisons = []
    for metric_name in _LATENCY_FIELDS:
        current = (report.get("metrics") or {}).get(metric_name) or {}
        baseline = (baseline_report.get("metrics") or {}).get(metric_name) or {}
        for stat_name in ("p50", "p95", "p99"):
            current_value = _number_or_none(current.get(stat_name))
            baseline_value = _number_or_none(baseline.get(stat_name))
            if current_value is None or baseline_value is None:
                continue
            delta_ms = current_value - baseline_value
            ratio = delta_ms / baseline_value if baseline_value else 0.0
            regressed = delta_ms > max_regression_ms and ratio > max_regression_ratio
            comparisons.append(
                {
                    "metric": metric_name,
                    "stat": stat_name,
                    "baseline": baseline_value,
                    "current": current_value,
                    "delta_ms": delta_ms,
                    "delta_ratio": ratio,
                    "regressed": regressed,
                }
            )
    return {
        "run_id": baseline_report.get("run_id"),
        "schema_version": baseline_report.get("schema_version"),
        "max_regression_ms": max_regression_ms,
        "max_regression_ratio": max_regression_ratio,
        "regressed": any(item["regressed"] for item in comparisons),
        "comparisons": comparisons,
    }


def render_benchmark_markdown(report: dict[str, Any]) -> str:
    metrics = report.get("metrics") or {}
    lines = [
        f"# Realtime Benchmark: {report.get('run_id')}",
        "",
        f"- schema: `{report.get('schema_version')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- mode: `{report.get('mode')}`",
        f"- profile: `{report.get('profile')}`",
        f"- passed: `{report.get('passed')}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in (report.get("summary") or {}).items():
        lines.append(f"- {key}: `{value}`")
    llm_summary = report.get("llm_summary") or {}
    if llm_summary:
        lines.extend(
            [
                "",
                "## LLM Summary",
                "",
                f"- status: `{llm_summary.get('status')}`",
                f"- model: `{llm_summary.get('model_id') or llm_summary.get('requested_model') or ''}`",
                "",
            ]
        )
        text = str(llm_summary.get("text") or llm_summary.get("error") or "").strip()
        if text:
            lines.extend([text, ""])
    lines.extend(["", "## Latency Metrics", "", "| metric | count | p50 | p95 | p99 | max | mean |", "|---|---:|---:|---:|---:|---:|---:|"])
    for field in _LATENCY_FIELDS:
        stat = metrics.get(field) or {}
        lines.append(
            "| "
            + " | ".join(
                [
                    field,
                    str(stat.get("count") or 0),
                    _fmt_ms(stat.get("p50")),
                    _fmt_ms(stat.get("p95")),
                    _fmt_ms(stat.get("p99")),
                    _fmt_ms(stat.get("max")),
                    _fmt_ms(stat.get("mean")),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Thresholds", "", "| check | passed | actual | threshold |", "|---|---:|---:|---:|"])
    for check in report.get("threshold_checks") or []:
        lines.append(
            f"| {check.get('name')} | {check.get('passed')} | "
            f"{_fmt_ms(check.get('actual'))} | {_fmt_ms(check.get('threshold'))} |"
        )
    baseline = report.get("baseline")
    if baseline:
        lines.extend(["", "## Baseline Comparison", "", f"- regressed: `{baseline.get('regressed')}`", "", "| metric | stat | baseline | current | delta | ratio | regressed |", "|---|---|---:|---:|---:|---:|---:|"])
        for item in baseline.get("comparisons") or []:
            lines.append(
                f"| {item['metric']} | {item['stat']} | {_fmt_ms(item['baseline'])} | "
                f"{_fmt_ms(item['current'])} | {_fmt_ms(item['delta_ms'])} | "
                f"{item['delta_ratio']:.1%} | {item['regressed']} |"
            )
    lines.extend(["", "## Scenarios", "", "| scenario | passed | turns |", "|---|---:|---:|"])
    for scenario in report.get("scenarios") or []:
        lines.append(
            f"| {scenario.get('scenario_id')} | {scenario.get('passed')} | "
            f"{scenario.get('turn_count')} |"
        )
    failed = report.get("failed_checks") or []
    if failed:
        lines.extend(["", "## Failed Checks", "", "| scenario | turn | check | detail |", "|---|---|---|---|"])
        for item in failed:
            lines.append(
                f"| {item.get('scenario_id')} | {item.get('turn_id')} | "
                f"{item.get('name')} | {item.get('detail') or ''} |"
            )
    lines.extend(["", "## Admin Read Contract", "", "- List: `GET /api/admin/reports?kind=realtime`", "- Detail: `GET /api/admin/reports/realtime/<filename>.json`", "- Primary chart data: `payload.metrics`, `payload.turns`, `payload.scenarios`, `payload.baseline`.", ""])
    return "\n".join(lines)


def render_benchmark_html(report: dict[str, Any]) -> str:
    title = f"Realtime Benchmark {report.get('run_id')}"
    turns = report.get("turns") or []
    max_total = max([_number_or_none(t.get("total_ms")) or 0 for t in turns] or [1])
    rows = "\n".join(_turn_row_html(turn, max_total=max_total) for turn in turns)
    cards = "\n".join(_metric_card_html(name, report.get("metrics", {}).get(name) or {}) for name in _LATENCY_FIELDS)
    summary_html = _llm_summary_html(report.get("llm_summary") or {})
    failed = report.get("failed_checks") or []
    failed_rows = "\n".join(
        "<tr>"
        f"<td>{_h(item.get('scenario_id'))}</td>"
        f"<td>{_h(item.get('turn_id'))}</td>"
        f"<td>{_h(item.get('name'))}</td>"
        f"<td>{_h(item.get('detail'))}</td>"
        "</tr>"
        for item in failed
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{_h(title)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #18212f; background: #f7f8fb; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .meta {{ color: #5c6678; margin-bottom: 24px; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; margin: 16px 0 28px; }}
    .card {{ background: white; border: 1px solid #dce1ea; border-radius: 8px; padding: 14px; }}
    .card b {{ display: block; font-size: 13px; color: #5c6678; }}
    .card span {{ display: block; font-size: 28px; margin-top: 6px; }}
    table {{ border-collapse: collapse; width: 100%; background: white; border: 1px solid #dce1ea; margin-bottom: 28px; }}
    th, td {{ text-align: left; border-bottom: 1px solid #e7ebf2; padding: 8px 10px; font-size: 14px; vertical-align: middle; }}
    th {{ background: #eef2f7; color: #3a4558; }}
    .pass {{ color: #0f7b4f; font-weight: 600; }}
    .fail {{ color: #b42318; font-weight: 600; }}
    .bar {{ width: 100%; height: 10px; background: #e7ebf2; border-radius: 5px; overflow: hidden; }}
    .bar span {{ display: block; height: 100%; background: #4578d4; }}
    .bar .first {{ background: #13a081; }}
    .subtle {{ color: #687386; }}
  </style>
</head>
<body>
  <h1>{_h(title)}</h1>
  <div class="meta">mode={_h(report.get('mode'))} profile={_h(report.get('profile'))} generated={_h(report.get('generated_at'))} passed=<b class="{ 'pass' if report.get('passed') else 'fail' }">{_h(report.get('passed'))}</b></div>
  {summary_html}
  <div class="cards">{cards}</div>
  <h2>Turns</h2>
  <table>
    <thead><tr><th>Scenario</th><th>Turn</th><th>Status</th><th>First Delta</th><th>Total</th><th>Latency</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <h2>Failed Checks</h2>
  <table>
    <thead><tr><th>Scenario</th><th>Turn</th><th>Check</th><th>Detail</th></tr></thead>
    <tbody>{failed_rows or '<tr><td colspan="4" class="subtle">None</td></tr>'}</tbody>
  </table>
</body>
</html>
"""


def write_benchmark_artifacts(
    report: dict[str, Any],
    *,
    output_json: Path,
    write_latest: bool = True,
) -> dict[str, str]:
    output_json = output_json.expanduser()
    output_json.parent.mkdir(parents=True, exist_ok=True)
    markdown_path = output_json.with_suffix(".md")
    html_path = output_json.with_suffix(".html")
    output_json.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_benchmark_markdown(report), encoding="utf-8")
    html_path.write_text(render_benchmark_html(report), encoding="utf-8")
    if write_latest:
        latest = output_json.parent / "latest.json"
        latest.write_text(output_json.read_text(encoding="utf-8"), encoding="utf-8")
        (output_json.parent / "latest.md").write_text(
            markdown_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (output_json.parent / "latest.html").write_text(
            html_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        mode = str(report.get("mode") or "").strip()
        if mode:
            safe_mode = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in mode)
            (output_json.parent / f"latest-{safe_mode}.json").write_text(
                output_json.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (output_json.parent / f"latest-{safe_mode}.md").write_text(
                markdown_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (output_json.parent / f"latest-{safe_mode}.html").write_text(
                html_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
    return {
        "json": str(output_json),
        "markdown": str(markdown_path),
        "html": str(html_path),
    }


def _normalize_turn(
    *,
    mode: str,
    scenario_id: str | None,
    turn: dict[str, Any],
    trace_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    checks = list(turn.get("checks") or [])
    failed_checks = [check for check in checks if not check.get("passed", True)]
    return {
        "mode": mode,
        "scenario_id": scenario_id or "default",
        "turn_id": str(turn.get("turn_id") or turn.get("logical_turn_id") or ""),
        "logical_turn_id": str(turn.get("logical_turn_id") or turn.get("turn_id") or ""),
        "input_preview": turn.get("input_preview") or turn.get("input_text", "")[:120],
        "assistant_preview": turn.get("assistant_preview") or turn.get("assistant_text", "")[:240],
        "first_delta_ms": _int_or_none(turn.get("first_delta_ms")),
        "total_ms": _int_or_none(turn.get("total_ms")),
        "passed": bool(turn.get("passed", not failed_checks)) and not failed_checks,
        "error": turn.get("error"),
        "event_kinds": list(turn.get("event_kinds") or []),
        "checks": checks,
        "failed_check_names": [check.get("name") for check in failed_checks],
        "trace_summary": trace_summary or {},
    }


def _latency_stats(values: list[Any]) -> dict[str, Any]:
    cleaned = [int(v) for v in values if isinstance(v, int | float)]
    if not cleaned:
        return {
            "count": 0,
            "min": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "max": None,
            "mean": None,
        }
    return {
        "count": len(cleaned),
        "min": min(cleaned),
        "p50": _percentile(cleaned, 0.50),
        "p90": _percentile(cleaned, 0.90),
        "p95": _percentile(cleaned, 0.95),
        "p99": _percentile(cleaned, 0.99),
        "max": max(cleaned),
        "mean": round(statistics.mean(cleaned), 1),
    }


def _percentile(values: list[int], pct: float) -> int:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, round((len(ordered) - 1) * pct))
    return ordered[idx]


def _threshold_checks(metrics: dict[str, Any], thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for metric_name, stat_name, threshold_key in (
        ("first_delta_ms", "p95", "first_delta_p95_ms"),
        ("first_delta_ms", "p99", "first_delta_p99_ms"),
        ("total_ms", "p95", "total_p95_ms"),
        ("total_ms", "p99", "total_p99_ms"),
    ):
        if threshold_key not in thresholds:
            continue
        actual = (metrics.get(metric_name) or {}).get(stat_name)
        threshold = thresholds[threshold_key]
        checks.append(
            {
                "name": threshold_key,
                "metric": metric_name,
                "stat": stat_name,
                "actual": actual,
                "threshold": threshold,
                "passed": actual is not None and actual <= threshold,
            }
        )
    return checks


def _dimension_metrics(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [{"name": "all", "filters": {}, "metrics": {}}]
    out[0]["metrics"] = {
        field: _latency_stats([turn.get(field) for turn in turns])
        for field in _LATENCY_FIELDS
    }
    scenario_ids = sorted({str(turn.get("scenario_id")) for turn in turns})
    for scenario_id in scenario_ids:
        subset = [turn for turn in turns if str(turn.get("scenario_id")) == scenario_id]
        out.append(
            {
                "name": f"scenario:{scenario_id}",
                "filters": {"scenario_id": scenario_id},
                "metrics": {
                    field: _latency_stats([turn.get(field) for turn in subset])
                    for field in _LATENCY_FIELDS
                },
            }
        )
    return out


def _metric_card_html(name: str, stat: dict[str, Any]) -> str:
    return (
        '<div class="card">'
        f"<b>{_h(name)} p95</b>"
        f"<span>{_fmt_ms(stat.get('p95'))}</span>"
        f"<div class=\"subtle\">p50 {_fmt_ms(stat.get('p50'))} / p99 {_fmt_ms(stat.get('p99'))}</div>"
        "</div>"
    )


def _llm_summary_html(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    text = str(summary.get("text") or summary.get("error") or "").strip()
    if not text:
        return ""
    status = summary.get("status") or "unknown"
    model = summary.get("model_id") or summary.get("requested_model") or ""
    paragraphs = "".join(
        f"<p>{_h(part)}</p>" for part in re_split_paragraphs(text)
    )
    return (
        '<section class="card" style="margin-bottom:24px">'
        f"<b>LLM Summary · {_h(status)} · {_h(model)}</b>"
        f"{paragraphs}"
        "</section>"
    )


def re_split_paragraphs(text: str) -> list[str]:
    return [part.strip() for part in text.split("\n\n") if part.strip()]


def _turn_row_html(turn: dict[str, Any], *, max_total: int) -> str:
    total = _number_or_none(turn.get("total_ms")) or 0
    first = _number_or_none(turn.get("first_delta_ms")) or 0
    total_width = int((total / max_total) * 100) if max_total else 0
    first_width = int((first / max_total) * 100) if max_total else 0
    status_class = "pass" if turn.get("passed") else "fail"
    status = "PASS" if turn.get("passed") else "FAIL"
    return (
        "<tr>"
        f"<td>{_h(turn.get('scenario_id'))}</td>"
        f"<td>{_h(turn.get('logical_turn_id') or turn.get('turn_id'))}</td>"
        f"<td class=\"{status_class}\">{status}</td>"
        f"<td>{_fmt_ms(turn.get('first_delta_ms'))}</td>"
        f"<td>{_fmt_ms(turn.get('total_ms'))}</td>"
        "<td>"
        f"<div class=\"bar\"><span style=\"width:{total_width}%\"></span></div>"
        f"<div class=\"bar\" style=\"margin-top:3px\"><span class=\"first\" style=\"width:{first_width}%\"></span></div>"
        "</td>"
        "</tr>"
    )


def _fmt_ms(value: Any) -> str:
    number = _number_or_none(value)
    return "-" if number is None else f"{number:g}ms"


def _int_or_none(value: Any) -> int | None:
    number = _number_or_none(value)
    return int(number) if number is not None else None


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None


def _h(value: Any) -> str:
    return html.escape("" if value is None else str(value))
