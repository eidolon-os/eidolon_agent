"""LLM-generated narrative summaries for benchmark reports."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.types.messages import ChatMessage, MessageRole

PROMPT_VERSION = "realtime-benchmark-summary.v1"


class BenchmarkReportSummarizer:
    """Ask the configured project LLM to explain a benchmark report."""

    def __init__(
        self,
        llm: LLMPort,
        *,
        max_failed_checks: int = 24,
        max_slow_turns: int = 16,
        max_dimension_rows: int = 16,
    ) -> None:
        self._llm = llm
        self._max_failed_checks = max_failed_checks
        self._max_slow_turns = max_slow_turns
        self._max_dimension_rows = max_dimension_rows

    async def summarize(
        self,
        report: dict[str, Any],
        *,
        max_tokens: int = 900,
        model: str | None = None,
    ) -> dict[str, Any]:
        diagnostic = build_llm_diagnostic_payload(
            report,
            max_failed_checks=self._max_failed_checks,
            max_slow_turns=self._max_slow_turns,
            max_dimension_rows=self._max_dimension_rows,
        )
        chunks: list[str] = []
        async for delta in self._llm.stream(
            _messages_for_summary(diagnostic),
            model=model,
            temperature=0.2,
            max_tokens=max_tokens,
            request_id=f"benchmark-summary-{report.get('run_id')}-{uuid.uuid4().hex[:8]}",
        ):
            if delta.text_delta:
                chunks.append(delta.text_delta)
            if delta.finish is not None:
                break
        text = _clean_summary("".join(chunks))
        return {
            "status": "ok" if text else "empty",
            "prompt_version": PROMPT_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model_id": getattr(self._llm, "model_id", None),
            "requested_model": model,
            "text": text,
            "diagnostic": diagnostic,
        }


def build_llm_diagnostic_payload(
    report: dict[str, Any],
    *,
    max_failed_checks: int = 24,
    max_slow_turns: int = 16,
    max_dimension_rows: int = 16,
) -> dict[str, Any]:
    """Build a compact prompt-safe diagnostic payload.

    Avoid sending raw user/assistant text to the summarizer. Scenario ids,
    check names, numeric details, and trace-derived labels are enough for a
    useful benchmark diagnosis.
    """
    turns = list(report.get("turns") or [])
    slow_turns = sorted(
        (
            {
                "scenario_id": turn.get("scenario_id"),
                "turn_id": turn.get("logical_turn_id") or turn.get("turn_id"),
                "passed": turn.get("passed"),
                "first_delta_ms": turn.get("first_delta_ms"),
                "total_ms": turn.get("total_ms"),
                "failed_check_names": turn.get("failed_check_names") or [],
                "event_kinds": turn.get("event_kinds") or [],
                "trace_summary": _compact_trace_summary(turn.get("trace_summary") or {}),
            }
            for turn in turns
        ),
        key=lambda item: (
            _number_or_zero(item.get("first_delta_ms")),
            _number_or_zero(item.get("total_ms")),
        ),
        reverse=True,
    )[:max_slow_turns]
    failed_checks = [
        {
            "scenario_id": item.get("scenario_id"),
            "turn_id": item.get("turn_id"),
            "name": item.get("name"),
            "detail": item.get("detail"),
        }
        for item in (report.get("failed_checks") or [])[:max_failed_checks]
    ]
    return {
        "schema_version": report.get("schema_version"),
        "run_id": report.get("run_id"),
        "mode": report.get("mode"),
        "profile": report.get("profile"),
        "passed": report.get("passed"),
        "summary": report.get("summary") or {},
        "metrics": report.get("metrics") or {},
        "thresholds": report.get("thresholds") or {},
        "threshold_failures": [
            check
            for check in (report.get("threshold_checks") or [])
            if not check.get("passed")
        ],
        "baseline": _compact_baseline(report.get("baseline") or {}),
        "failed_checks": failed_checks,
        "slow_turns": slow_turns,
        "dimensions": _top_dimensions(report.get("dimensions") or [], max_dimension_rows),
    }


def _messages_for_summary(payload: dict[str, Any]) -> list[ChatMessage]:
    now = datetime.now(timezone.utc)
    system = (
        "你是 eidolon_agent 的 realtime benchmark 诊断助手。"
        "你会收到一份机器可读 benchmark 诊断 JSON。"
        "请输出中文、面向工程师的人类可读摘要。"
        "必须包含：1) 总体结论；2) 不达标项；3) 可能原因；4) 下一步建议。"
        "重点关注 realtime 首响、总耗时、阈值失败、失败的 fixture/check、baseline 回退。"
        "不要编造 JSON 中没有的事实；如果证据不足，请明确说需要进一步 trace/log。"
        "不要输出原始 JSON；控制在 600 字以内。"
    )
    user = "请分析这份 benchmark 诊断：\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    )
    return [
        ChatMessage(id=uuid.uuid4().hex, role=MessageRole.SYSTEM, content=system, created_at=now),
        ChatMessage(id=uuid.uuid4().hex, role=MessageRole.USER, content=user, created_at=now),
    ]


def _top_dimensions(dimensions: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows = []
    for item in dimensions:
        metrics = item.get("metrics") or {}
        first = metrics.get("first_delta_ms") or {}
        total = metrics.get("total_ms") or {}
        rows.append(
            {
                "name": item.get("name"),
                "filters": item.get("filters") or {},
                "first_delta_p95_ms": first.get("p95"),
                "first_delta_p99_ms": first.get("p99"),
                "total_p95_ms": total.get("p95"),
                "total_p99_ms": total.get("p99"),
            }
        )
    return sorted(rows, key=lambda row: _number_or_zero(row.get("first_delta_p95_ms")), reverse=True)[
        :limit
    ]


def _compact_baseline(baseline: dict[str, Any]) -> dict[str, Any] | None:
    if not baseline:
        return None
    return {
        "run_id": baseline.get("run_id"),
        "regressed": baseline.get("regressed"),
        "regressions": [
            item for item in (baseline.get("comparisons") or []) if item.get("regressed")
        ],
    }


def _compact_trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "privacy": trace.get("privacy"),
        "context": trace.get("context") or {},
        "memory_recall": {
            key: value
            for key, value in (trace.get("memory_recall") or {}).items()
            if key in {"attempted", "degraded", "degraded_reason", "context_injected"}
        },
        "memory_write": {
            key: value
            for key, value in (trace.get("memory_write") or {}).items()
            if key in {"disposition", "fanout_allowed", "skipped_reason"}
        },
        "tools": trace.get("tools") or {},
    }


def _clean_summary(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _number_or_zero(value: Any) -> float:
    return float(value) if isinstance(value, int | float) else 0.0
