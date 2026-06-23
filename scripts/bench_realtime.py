"""Unified realtime benchmark runner.

The runner emits one stable report schema for local hot-path checks,
in-process replay, live gRPC latency, and live-service replay.

Examples:
    python scripts/bench_realtime.py --mode hotpath --turns 30
    python scripts/bench_realtime.py --mode in-process
    python scripts/bench_realtime.py --mode live-grpc --turns 20 --reuse-stream
    python scripts/bench_realtime.py --mode live-service --fixture tests/benchmark/fixtures/live_service_smoke.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from bench_chat import (
    _bench_one_turn_per_stream,
    _bench_reused_stream,
)
from bench_chat import (
    _issue_token as _issue_chat_token,
)
from replay_live_service import (
    _issue_token as _issue_live_token,
)
from replay_live_service import (
    _run_scenarios as _run_live_service_scenarios,
)
from replay_live_service import (
    _startup_failure_report,
    ensure_registry_user,
)

from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config import load_settings
from eidolon_agent.infra.benchmark import (
    BenchmarkReportSummarizer,
    build_realtime_benchmark_report,
    write_benchmark_artifacts,
)
from eidolon_agent.infra.benchmark.reporting import (
    normalize_experience_report,
    normalize_flat_turns,
    normalize_live_service_report,
)
from eidolon_agent.app.benchmark import load_replay_scenarios
from eidolon_agent.app.benchmark.experience import ExperienceReplayRunner

DEFAULT_OUTPUT_DIR = Path("~/eidolon/debug/reports/realtime")
DEFAULT_IN_PROCESS_FIXTURE = Path("tests/benchmark/fixtures/core_experience.jsonl")
DEFAULT_LIVE_SERVICE_FIXTURE = Path("tests/benchmark/fixtures/live_service_smoke.jsonl")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("hotpath", "in-process", "live-grpc", "live-service"),
        default="in-process",
        help="Benchmark tier to run.",
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--profile", default="voice")
    parser.add_argument("--fixture", action="append", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--no-latest", action="store_true")
    parser.add_argument(
        "--llm-summary",
        action="store_true",
        help="Use the configured project LLM to add a human-readable diagnosis to the report.",
    )
    parser.add_argument(
        "--llm-summary-model",
        default=None,
        help="Optional configured model name to use for the benchmark diagnosis.",
    )
    parser.add_argument("--llm-summary-max-tokens", type=int, default=900)
    parser.add_argument(
        "--allow-fake-llm-summary",
        action="store_true",
        help="Allow the fake test provider for summary generation. Disabled by default.",
    )
    parser.add_argument("--first-delta-p95-ms", type=int, default=None)
    parser.add_argument("--first-delta-p99-ms", type=int, default=None)
    parser.add_argument("--total-p95-ms", type=int, default=None)
    parser.add_argument("--total-p99-ms", type=int, default=None)
    parser.add_argument("--baseline-max-regression-ms", type=int, default=150)
    parser.add_argument("--baseline-max-regression-ratio", type=float, default=0.25)

    parser.add_argument("--http", default="http://127.0.0.1:8081")
    parser.add_argument("--grpc", default="127.0.0.1:45051")
    parser.add_argument("--registry-http", default=None)
    parser.add_argument("--provision-user", action="store_true")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--user", default=None)
    parser.add_argument("--template", default="caretaker_jiezhi")
    parser.add_argument("--conversation", default=None)
    parser.add_argument("--turns", type=int, default=10)
    parser.add_argument("--reuse-stream", action="store_true")
    parser.add_argument("--admin-delay-s", type=float, default=0.2)
    parser.add_argument("--admin-timeout-s", type=float, default=5.0)
    parser.add_argument("--http-timeout-s", type=float, default=20.0)
    args = parser.parse_args()

    run_id = args.run_id or _default_run_id(args.mode)
    thresholds = _thresholds_for(args)
    target = {
        "mode": args.mode,
        "http_base": args.http if args.mode.startswith("live") else None,
        "grpc_target": args.grpc if args.mode.startswith("live") else None,
        "fixture_paths": [str(p) for p in _fixture_paths(args)],
        "turns": args.turns,
        "reuse_stream": args.reuse_stream,
    }

    source_report: dict[str, Any]
    if args.mode == "hotpath":
        source_report = await _run_hotpath(args.turns)
        scenarios, turns = normalize_experience_report(source_report, mode=args.mode)
    elif args.mode == "in-process":
        source_report = await _run_in_process(_fixture_paths(args))
        scenarios, turns = normalize_experience_report(source_report, mode=args.mode)
    elif args.mode == "live-grpc":
        source_report = await _run_live_grpc(args)
        scenarios, turns = normalize_flat_turns(
            source_report["turns"],
            mode=args.mode,
            scenario_id="live-grpc-latency",
            description="Real gRPC bidi latency benchmark.",
        )
    else:
        source_report = await _run_live_service(args, _fixture_paths(args))
        scenarios, turns = normalize_live_service_report(source_report, mode=args.mode)

    baseline_report = (
        json.loads(args.baseline.expanduser().read_text(encoding="utf-8"))
        if args.baseline is not None
        else None
    )
    report = build_realtime_benchmark_report(
        run_id=run_id,
        mode=args.mode,
        profile=args.profile,
        target=target,
        thresholds=thresholds,
        scenarios=scenarios,
        turns=turns,
        source_reports=[_source_report_summary(source_report)],
        baseline_report=baseline_report,
    )
    if args.llm_summary:
        report["llm_summary"] = await _generate_llm_summary(report, args)
    output_json = args.output_dir.expanduser() / f"benchmark-{run_id}.json"
    artifacts = write_benchmark_artifacts(
        report,
        output_json=output_json,
        write_latest=not args.no_latest,
    )
    _print_summary(report, artifacts)
    return 0 if report["passed"] else 1


async def _run_hotpath(turns: int) -> dict[str, Any]:
    scenario = {
        "id": "hotpath-framework",
        "description": "Pure in-process TurnEngine path with fake LLM and no external services.",
        "turns": [
            {
                "turn_id": f"hotpath-{idx + 1}",
                "user": "你好，给我一句简短回应。",
            }
            for idx in range(turns)
        ],
    }
    return await ExperienceReplayRunner().run_many([scenario])


async def _run_in_process(fixtures: list[Path]) -> dict[str, Any]:
    scenarios = load_replay_scenarios(fixtures)
    return await ExperienceReplayRunner().run_many(scenarios)


async def _run_live_grpc(args: argparse.Namespace) -> dict[str, Any]:
    user_id = args.user or f"bench-{uuid.uuid4().hex[:8]}"
    conversation_id = args.conversation or f"bench-{uuid.uuid4().hex[:8]}"
    async with httpx.AsyncClient(timeout=args.http_timeout_s, trust_env=False) as http:
        provisioning = None
        if args.provision_user:
            provisioning = await ensure_registry_user(
                http=http,
                registry_base=args.registry_http,
                tenant_id=args.tenant,
                user_id=user_id,
            )
        token, resolved_user_id = await _issue_chat_token(
            http,
            args.grpc,
            args.http,
            args.tenant,
            user_id,
            args.template,
        )
    if args.reuse_stream:
        rows = await _bench_reused_stream(args.grpc, token, args.turns, conversation_id)
    else:
        rows = await _bench_one_turn_per_stream(
            args.grpc,
            token,
            args.turns,
            conversation_id,
        )
    turns = []
    for idx, row in enumerate(rows, 1):
        error = row.get("error")
        turns.append(
            {
                "turn_id": str(row.get("turn_id") or f"live-grpc-{idx}"),
                "logical_turn_id": f"live-grpc-{idx}",
                "first_delta_ms": row.get("first_delta_ms"),
                "total_ms": row.get("total_ms"),
                "passed": error is None and row.get("total_ms") is not None,
                "error": error,
                "checks": [
                    {
                        "name": "stream_done",
                        "passed": row.get("total_ms") is not None,
                        "detail": error or "",
                    }
                ],
            }
        )
    return {
        "schema_version": "eidolon_agent.live_grpc_benchmark_source.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": all(turn["passed"] for turn in turns),
        "tenant_id": args.tenant,
        "user_id": resolved_user_id,
        "conversation_id": conversation_id,
        "provisioning": provisioning,
        "target": {"http_base": args.http, "grpc_target": args.grpc},
        "turns": turns,
    }


async def _run_live_service(
    args: argparse.Namespace,
    fixtures: list[Path],
) -> dict[str, Any]:
    scenarios = load_replay_scenarios(fixtures)
    user_id = args.user or f"replay-live-{uuid.uuid4().hex[:8]}"
    try:
        async with httpx.AsyncClient(timeout=args.http_timeout_s, trust_env=False) as http:
            provisioning = None
            if args.provision_user:
                provisioning = await ensure_registry_user(
                    http=http,
                    registry_base=args.registry_http,
                    tenant_id=args.tenant,
                    user_id=user_id,
                )
            token, user_id = await _issue_live_token(
                http=http,
                http_base=args.http,
                grpc_target=args.grpc,
                tenant_id=args.tenant,
                user_id=user_id,
                template_id=args.template,
            )
            report = await _run_live_service_scenarios(
                scenarios=scenarios,
                http=http,
                http_base=args.http,
                grpc_target=args.grpc,
                token=token,
                tenant_id=args.tenant,
                user_id=user_id,
                conversation_id=args.conversation or f"replay-live-{uuid.uuid4().hex[:8]}",
                admin_delay_s=args.admin_delay_s,
                admin_timeout_s=args.admin_timeout_s,
            )
            report["provisioning"] = provisioning
            return report
    except Exception as exc:
        return _startup_failure_report(
            scenarios=scenarios,
            http_base=args.http,
            grpc_target=args.grpc,
            tenant_id=args.tenant,
            user_id=user_id,
            error=exc,
        )


def _fixture_paths(args: argparse.Namespace) -> list[Path]:
    if args.fixture:
        return list(args.fixture)
    if args.mode == "live-service":
        return [DEFAULT_LIVE_SERVICE_FIXTURE]
    if args.mode == "in-process":
        return [DEFAULT_IN_PROCESS_FIXTURE]
    return []


def _thresholds_for(args: argparse.Namespace) -> dict[str, Any]:
    live = args.mode.startswith("live")
    defaults = {
        "first_delta_p95_ms": 3000 if live else 300,
        "first_delta_p99_ms": 5000 if live else 500,
        "total_p95_ms": 20000 if live else 500,
        "total_p99_ms": 30000 if live else 900,
    }
    return {
        "first_delta_p95_ms": args.first_delta_p95_ms or defaults["first_delta_p95_ms"],
        "first_delta_p99_ms": args.first_delta_p99_ms or defaults["first_delta_p99_ms"],
        "total_p95_ms": args.total_p95_ms or defaults["total_p95_ms"],
        "total_p99_ms": args.total_p99_ms or defaults["total_p99_ms"],
        "baseline_max_regression_ms": args.baseline_max_regression_ms,
        "baseline_max_regression_ratio": args.baseline_max_regression_ratio,
    }


def _default_run_id(mode: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{mode}-{stamp}-{uuid.uuid4().hex[:6]}"


def _source_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": report.get("schema_version"),
        "generated_at": report.get("generated_at"),
        "passed": report.get("passed"),
        "summary": report.get("summary"),
        "metrics": report.get("metrics"),
    }


async def _generate_llm_summary(
    report: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    router = None
    try:
        router = _build_llm_router(load_settings())
        if router.model_id == "fake" and not args.allow_fake_llm_summary:
            return {
                "status": "skipped",
                "error": (
                    "configured default LLM resolved to fake; pass a real model in settings "
                    "or use --allow-fake-llm-summary for tests"
                ),
                "model_id": router.model_id,
            }
        return await BenchmarkReportSummarizer(router).summarize(
            report,
            model=args.llm_summary_model,
            max_tokens=args.llm_summary_max_tokens,
        )
    except Exception as exc:
        return {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if router is not None:
            await router.close()


def _print_summary(report: dict[str, Any], artifacts: dict[str, str]) -> None:
    first = report["metrics"]["first_delta_ms"]
    total = report["metrics"]["total_ms"]
    print(f"run_id={report['run_id']} mode={report['mode']} passed={report['passed']}")
    print(
        "first_delta_ms "
        f"p50={first.get('p50')} p95={first.get('p95')} p99={first.get('p99')}"
    )
    print(
        "total_ms       "
        f"p50={total.get('p50')} p95={total.get('p95')} p99={total.get('p99')}"
    )
    print(f"json={artifacts['json']}")
    print(f"markdown={artifacts['markdown']}")
    print(f"html={artifacts['html']}")
    llm_summary = report.get("llm_summary") or {}
    if llm_summary:
        print(f"llm_summary={llm_summary.get('status')}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
