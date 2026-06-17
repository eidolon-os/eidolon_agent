from __future__ import annotations

from eidolon_agent.infra.benchmark.summary import (
    BenchmarkReportSummarizer,
    build_llm_diagnostic_payload,
)
from eidolon_agent.infra.llm.providers.fake import FakeLLM


def test_diagnostic_payload_omits_raw_turn_text() -> None:
    report = {
        "schema_version": "eidolon_agent.realtime_benchmark_report.v1",
        "run_id": "unit",
        "mode": "live-service",
        "profile": "voice",
        "passed": False,
        "summary": {"turn_count": 1, "failed_check_count": 1},
        "metrics": {"first_delta_ms": {"p95": 4000}, "total_ms": {"p95": 6000}},
        "threshold_checks": [
            {
                "name": "first_delta_p95_ms",
                "passed": False,
                "actual": 4000,
                "threshold": 3000,
            }
        ],
        "failed_checks": [
            {
                "scenario_id": "privacy",
                "turn_id": "t1",
                "name": "forbidden_assistant:secret",
                "detail": "got=secret",
            }
        ],
        "turns": [
            {
                "scenario_id": "privacy",
                "turn_id": "t1",
                "logical_turn_id": "privacy-1",
                "passed": False,
                "first_delta_ms": 4000,
                "total_ms": 6000,
                "input_preview": "raw user text must not be sent",
                "assistant_preview": "raw assistant text must not be sent",
                "failed_check_names": ["forbidden_assistant:secret"],
                "trace_summary": {
                    "privacy": "private",
                    "memory_recall": {"attempted": True, "degraded": False},
                },
            }
        ],
    }

    payload = build_llm_diagnostic_payload(report)

    assert "input_preview" not in str(payload)
    assert "assistant_preview" not in str(payload)
    assert payload["threshold_failures"][0]["name"] == "first_delta_p95_ms"
    assert payload["slow_turns"][0]["turn_id"] == "privacy-1"


async def test_summarizer_returns_llm_summary() -> None:
    llm = FakeLLM(
        script=[{"kind": "text", "text": "总体结论：首响超阈值。建议先看 LLM TTFT。"}],
        per_token_delay_s=0,
    )
    summarizer = BenchmarkReportSummarizer(llm)

    summary = await summarizer.summarize(
        {
            "run_id": "unit",
            "mode": "live-grpc",
            "passed": False,
            "summary": {},
            "metrics": {},
            "threshold_checks": [],
            "turns": [],
        }
    )

    assert summary["status"] == "ok"
    assert "首响超阈值" in summary["text"]
    assert summary["prompt_version"] == "realtime-benchmark-summary.v1"
