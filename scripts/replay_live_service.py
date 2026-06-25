"""Run replay fixtures against a running real agent service.

This is the service/smoke tier: it uses real gRPC chat, real admin HTTP, real
SQLite turn trace, and whatever real memory/NATS/LLM configuration the running
agent has. It intentionally does not inspect prompt text.

Examples:
    python scripts/replay_live_service.py --fixture tests/benchmark/fixtures/live_service_smoke.jsonl
    python scripts/replay_live_service.py --output ~/eidolon/debug/reports/replay/live.json --markdown live.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from eidolon_sdk.core.grpc import authorization_metadata, create_aio_channel

from eidolon_agent.app.benchmark import (
    load_replay_scenarios,
    render_replay_html,
    render_replay_markdown,
)
from eidolon_agent.app.benchmark.suites import (
    LIVE_AGENT_MEMORY_BENCHMARK_NAME,
    live_agent_memory_experience_scenarios,
)
from eidolon_agent.app.transport.grpc.proto import pb, pbg
from eidolon_agent.infra.benchmark.users import (
    DEFAULT_BENCHMARK_TENANT_ID,
    DEFAULT_BENCHMARK_USER_ID,
    resolve_benchmark_identity,
)

DEFAULT_REPORT = Path("~/eidolon/debug/reports/replay/live-service-latest.json")
DEFAULT_REGISTRY_HTTP = (
    os.getenv("EIDOLON_BENCHMARK_REGISTRY_HTTP") or "http://127.0.0.1:9000/api"
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", action="append", type=Path, default=[])
    parser.add_argument(
        "--agent-memory-benchmark",
        action="store_true",
        help=(
            "Run the live real-service benchmark for agent + memory experience "
            f"({LIVE_AGENT_MEMORY_BENCHMARK_NAME})."
        ),
    )
    parser.add_argument("--http", default="http://127.0.0.1:8081")
    parser.add_argument("--grpc", default="127.0.0.1:45051")
    parser.add_argument(
        "--registry-http",
        default=DEFAULT_REGISTRY_HTTP,
        help=(
            "Central eidolon_admin API base including /api, e.g. "
            "http://127.0.0.1:9000/api. Used with --provision-user."
        ),
    )
    parser.add_argument(
        "--provision-user",
        dest="provision_user",
        action="store_true",
        default=True,
        help="Ensure the replay user exists through eidolon_admin /api/users before pairing.",
    )
    parser.add_argument(
        "--no-provision-user",
        dest="provision_user",
        action="store_false",
        help="Skip eidolon_admin user provisioning; pairing must already work for the user.",
    )
    parser.add_argument("--tenant", default=DEFAULT_BENCHMARK_TENANT_ID)
    parser.add_argument("--user", default=DEFAULT_BENCHMARK_USER_ID)
    parser.add_argument(
        "--allow-non-benchmark-user",
        action="store_true",
        help="Allow an explicit user_id that does not start with 'benchmark'.",
    )
    parser.add_argument("--template", default="caretaker_jiezhi")
    parser.add_argument("--conversation", default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT,
        help="JSON report path. Defaults to the agent admin reports directory.",
    )
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--html", type=Path, default=None)
    parser.add_argument("--admin-delay-s", type=float, default=0.2)
    parser.add_argument("--admin-timeout-s", type=float, default=5.0)
    parser.add_argument("--http-timeout-s", type=float, default=20.0)
    args = parser.parse_args()
    try:
        identity = resolve_benchmark_identity(
            tenant_id=args.tenant,
            user_id=args.user,
            allow_non_benchmark_user=args.allow_non_benchmark_user,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.tenant = identity.tenant_id
    args.user = identity.user_id

    if args.agent_memory_benchmark:
        scenarios = live_agent_memory_experience_scenarios()
    else:
        fixtures = args.fixture or [Path("tests/benchmark/fixtures/live_service_smoke.jsonl")]
        scenarios = load_replay_scenarios(fixtures)
    user_id = args.user
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
            token, user_id = await _issue_token(
                http=http,
                http_base=args.http,
                grpc_target=args.grpc,
                tenant_id=args.tenant,
                user_id=user_id,
                template_id=args.template,
            )
            report = await _run_scenarios(
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
    except Exception as exc:
        report = _startup_failure_report(
            scenarios=scenarios,
            http_base=args.http,
            grpc_target=args.grpc,
            tenant_id=args.tenant,
            user_id=user_id,
            error=exc,
        )

    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        output = args.output.expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"wrote live replay report to {output}")
    else:
        print(text)
    markdown = args.markdown.expanduser() if args.markdown is not None else None
    if markdown is None and args.output is not None:
        markdown = args.output.expanduser().with_suffix(".md")
    if markdown is not None:
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(render_replay_markdown(report), encoding="utf-8")
        print(f"wrote readable report to {markdown}")
    html = args.html.expanduser() if args.html is not None else None
    if html is None and args.output is not None:
        html = args.output.expanduser().with_suffix(".html")
    if html is not None:
        html.parent.mkdir(parents=True, exist_ok=True)
        html.write_text(render_replay_html(report), encoding="utf-8")
        print(f"wrote HTML report to {html}")
    return 0 if report["passed"] else 1


async def ensure_registry_user(
    *,
    http: httpx.AsyncClient,
    registry_base: str | None,
    tenant_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Create the memory/admin user through eidolon_admin when absent.

    This is the real provisioning path: eidolon_admin owns tenant metadata
    and the shared registry. The live replay script only orchestrates that
    public surface; it never edits registry storage directly.
    """
    if not registry_base:
        raise ValueError("--provision-user requires --registry-http")
    base = registry_base.rstrip("/")
    get_resp = await _request_with_retries(
        http,
        "GET",
        f"{base}/users/{user_id}",
        timeout=10.0,
    )
    if get_resp.status_code == 200:
        body = get_resp.json()
        return {
            "status": "exists",
            "user_id": user_id,
            "health": body.get("health"),
            "mcp_http_url": body.get("mcp_http_url"),
        }
    if get_resp.status_code != 404:
        get_resp.raise_for_status()

    create_resp = await _request_with_retries(
        http,
        "POST",
        f"{base}/users",
        json={
            "user_id": user_id,
            "tenant_id": tenant_id,
            "display_name": user_id,
        },
        timeout=60.0,
    )
    if create_resp.status_code == 409:
        return {"status": "already_exists_conflict", "user_id": user_id}
    create_resp.raise_for_status()
    body = create_resp.json()
    return {
        "status": "created",
        "user_id": user_id,
        "health": body.get("health"),
        "mcp_http_url": body.get("mcp_http_url"),
    }


async def _issue_token(
    *,
    http: httpx.AsyncClient,
    http_base: str,
    grpc_target: str,
    tenant_id: str,
    user_id: str,
    template_id: str,
) -> tuple[str, str]:
    resp = await _request_with_retries(
        http,
        "POST",
        f"{http_base}/api/admin/pairing/codes",
        json={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "default_template_id": template_id,
        },
    )
    resp.raise_for_status()
    code = resp.json()["code"]
    async with create_aio_channel(grpc_target) as channel:
        stub = pbg.EidolonAgentStub(channel)
        exchanged = await stub.ExchangePairingCode(
            pb.ExchangeRequest(
                pairing_code=code,
                device_id=f"replay-live-{uuid.uuid4().hex[:8]}",
                device_name="replay_live_service.py",
            )
        )
    return exchanged.device_token, exchanged.user_id


async def _run_scenarios(
    *,
    scenarios: list[dict[str, Any]],
    http: httpx.AsyncClient,
    http_base: str,
    grpc_target: str,
    token: str,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    admin_delay_s: float,
    admin_timeout_s: float,
) -> dict[str, Any]:
    metadata = authorization_metadata(token)
    scenario_reports = []
    async with create_aio_channel(grpc_target) as channel:
        stub = pbg.EidolonAgentStub(channel)
        for scenario in scenarios:
            scenario_id = str(scenario.get("id") or uuid.uuid4().hex[:8])
            scenario_reports.append(
                await _run_scenario(
                    scenario=scenario,
                    scenario_id=scenario_id,
                    stub=stub,
                    metadata=metadata,
                    http=http,
                    http_base=http_base,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    conversation_id=str(
                        scenario.get("conversation_id") or f"{conversation_id}-{scenario_id}"
                    ),
                    admin_delay_s=admin_delay_s,
                    admin_timeout_s=admin_timeout_s,
                )
            )
    turns = [turn for s in scenario_reports for turn in s["turns"]]
    first = [t["first_delta_ms"] for t in turns if t.get("first_delta_ms") is not None]
    total = [t["total_ms"] for t in turns if t.get("total_ms") is not None]
    categories = _category_metrics(scenario_reports)
    return {
        "schema_version": "eidolon_agent.live_service_replay_report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live_service",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "target": {
            "http_base": http_base,
            "grpc_target": grpc_target,
        },
        "passed": all(s["passed"] for s in scenario_reports),
        "summary": {
            "scenario_count": len(scenario_reports),
            "passed": sum(1 for s in scenario_reports if s["passed"]),
            "failed": sum(1 for s in scenario_reports if not s["passed"]),
        },
        "metrics": {
            "turn_count": len(turns),
            "check_count": _check_count(scenario_reports),
            "check_pass_rate": _check_pass_rate(scenario_reports),
            "scenario_pass_rate": _ratio(
                sum(1 for s in scenario_reports if s["passed"]),
                len(scenario_reports),
            ),
            "categories": categories,
            "first_delta_ms": {
                "p50": _median(first),
                "p95": _p95(first),
                "max": max(first) if first else None,
            },
            "total_ms": {
                "p50": _median(total),
                "p95": _p95(total),
                "max": max(total) if total else None,
            },
            "first_delta_p50_ms": _median(first),
            "first_delta_p95_ms": _p95(first),
            "total_p50_ms": _median(total),
            "total_p95_ms": _p95(total),
        },
        "scenarios": scenario_reports,
    }


async def _run_scenario(
    *,
    scenario: dict[str, Any],
    scenario_id: str,
    stub,
    metadata,
    http: httpx.AsyncClient,
    http_base: str,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    admin_delay_s: float,
    admin_timeout_s: float,
) -> dict[str, Any]:
    turn_reports = []
    for idx, turn in enumerate(scenario.get("turns") or []):
        logical_turn_id = str(turn.get("turn_id") or f"{scenario_id}-{idx + 1}")
        turn_id = f"{logical_turn_id}-{uuid.uuid4().hex[:6]}"
        expect = _merge_expectations(
            scenario.get("default_turn_expect") or {},
            turn.get("expect") or {},
        )
        turn_reports.append(
            await _run_turn(
                stub=stub,
                metadata=metadata,
                http=http,
                http_base=http_base,
                conversation_id=conversation_id,
                turn_id=turn_id,
                logical_turn_id=logical_turn_id,
                text=str(turn.get("user") or ""),
                turn_metadata=dict(turn.get("metadata") or {}),
                realtime=dict(turn.get("realtime") or {}),
                expect=expect,
                admin_delay_s=admin_delay_s,
                admin_timeout_s=admin_timeout_s,
            )
        )
    scenario_checks = _scenario_checks(scenario.get("expect") or {}, turn_reports)
    return {
        "scenario_id": scenario_id,
        "description": scenario.get("description") or "",
        "category": scenario.get("category") or "uncategorized",
        "tags": list(scenario.get("tags") or []),
        "passed": all(t["passed"] for t in turn_reports)
        and all(c["passed"] for c in scenario_checks),
        "turns": turn_reports,
        "checks": scenario_checks,
        "conversation_id": conversation_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
    }


async def _run_turn(
    *,
    stub,
    metadata,
    http: httpx.AsyncClient,
    http_base: str,
    conversation_id: str,
    turn_id: str,
    logical_turn_id: str,
    text: str,
    turn_metadata: dict[str, Any],
    realtime: dict[str, Any],
    expect: dict[str, Any],
    admin_delay_s: float,
    admin_timeout_s: float,
) -> dict[str, Any]:
    started = time.monotonic()
    assistant_parts: list[str] = []
    events: list[dict[str, Any]] = []
    first_delta_ms: int | None = None
    total_ms: int | None = None
    error: str | None = None

    async def _requests():
        start = pb.StartTurn(
            turn_id=turn_id,
            conversation_id=conversation_id,
            text=text,
        )
        if turn_metadata:
            start.metadata.update(turn_metadata)
        if realtime:
            start.realtime.update(realtime)
        yield pb.ChatRequest(start=start)
        while True:
            await asyncio.sleep(3600)

    call = stub.Chat(_requests(), metadata=metadata)
    try:
        async for ev in call:
            if ev.turn_id != turn_id:
                continue
            kind = pb.TurnEvent.Kind.Name(ev.kind)
            data = dict(ev.data)
            events.append({"kind": kind, "data": data})
            if kind == "DELTA":
                if first_delta_ms is None:
                    first_delta_ms = int((time.monotonic() - started) * 1000)
                assistant_parts.append(str(data.get("text") or ""))
            elif kind == "DONE":
                total_ms = int((time.monotonic() - started) * 1000)
                break
            elif kind == "ERROR":
                total_ms = int((time.monotonic() - started) * 1000)
                error = str(data.get("message") or data.get("code") or "error")
                break
    except Exception as exc:
        total_ms = int((time.monotonic() - started) * 1000)
        error = f"{type(exc).__name__}: {exc}"
    finally:
        call.cancel()

    await asyncio.sleep(admin_delay_s)
    detail = await _fetch_turn_detail(
        http=http,
        http_base=http_base,
        turn_id=turn_id,
        timeout_s=admin_timeout_s,
    )
    checks = _turn_checks(
        expect=expect,
        assistant_text="".join(assistant_parts),
        events=events,
        detail=detail,
        first_delta_ms=first_delta_ms,
        total_ms=total_ms,
    )
    return {
        "turn_id": turn_id,
        "logical_turn_id": logical_turn_id,
        "input_preview": text[:80],
        "assistant_preview": "".join(assistant_parts)[:240],
        "first_delta_ms": first_delta_ms,
        "total_ms": total_ms,
        "error": error,
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "event_kinds": [e["kind"] for e in events],
        "admin": {
            "status": detail.get("status"),
            "triage_kind": detail.get("triage_kind"),
            "observability_summary": detail.get("observability_summary"),
        },
    }


async def _fetch_turn_detail(
    *,
    http: httpx.AsyncClient,
    http_base: str,
    turn_id: str,
    timeout_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_error: str | None = None
    while True:
        try:
            resp = await _request_with_retries(
                http,
                "GET",
                f"{http_base}/api/admin/conversations/turns/{turn_id}",
            )
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            return {
                "status": "admin_detail_unavailable",
                "observability_summary": None,
                "error": last_error or "timeout",
        }
        await asyncio.sleep(0.1)


async def _request_with_retries(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    attempts: int = 5,
    retry_delay_s: float = 0.25,
    **kwargs: Any,
) -> httpx.Response:
    transient = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.ReadTimeout)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await http.request(method, url, **kwargs)
        except transient as exc:
            last_error = exc
            if attempt == attempts:
                break
            await asyncio.sleep(retry_delay_s * attempt)
    assert last_error is not None
    raise last_error


def _turn_checks(
    *,
    expect: dict[str, Any],
    assistant_text: str,
    events: list[dict[str, Any]],
    detail: dict[str, Any],
    first_delta_ms: int | None,
    total_ms: int | None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail_text: str = "") -> None:
        checks.append({"name": name, "passed": passed, "detail": detail_text})

    obs = detail.get("observability_summary") or {}
    memory_write = obs.get("memory_write") or {}
    memory = obs.get("memory") or {}
    context = obs.get("context") or {}
    tools = obs.get("tools") or {}
    guards = obs.get("development_guards") or {}
    privacy_mode = obs.get("privacy_mode")
    context_tags = obs.get("context_tags") or []

    add("stream_done", total_ms is not None)
    add("admin_trace_available", bool(obs), detail.get("error") or "")
    for item in expect.get("required_assistant_substrings") or []:
        add(f"required_assistant:{item}", item in assistant_text)
    for item in expect.get("forbidden_assistant_substrings") or []:
        add(f"forbidden_assistant:{item}", item not in assistant_text)
    if expect.get("no_tool_calls"):
        called = [e for e in events if e["kind"] == "TOOL_CALL"]
        add("no_tool_calls", not called, f"got={len(called)}")
    if expect.get("current_request_authority"):
        add(
            "current_request_authority",
            obs.get("context_structure_version") == "context_structure.v2"
            and any(
                isinstance(tag, dict)
                and tag.get("kind") == "current_user"
                and tag.get("authority") == "current_request"
                and tag.get("actionability") == "may_execute"
                for tag in context_tags
            ),
        )
    if expect.get("background_non_actionable"):
        add(
            "background_non_actionable",
            any(
                isinstance(tag, dict)
                and tag.get("kind") == "history"
                and tag.get("authority") == "background"
                and tag.get("actionability") == "must_not_execute"
                for tag in context_tags
            ),
        )
    if "context_structure_version" in expect:
        add(
            "context_structure_version",
            obs.get("context_structure_version") == expect["context_structure_version"],
            f"got={obs.get('context_structure_version')}",
        )
    if "history_presentation" in expect:
        add(
            "history_presentation",
            obs.get("history_presentation") == expect["history_presentation"],
            f"got={obs.get('history_presentation')}",
        )
    if "memory_write_disposition" in expect:
        add(
            "memory_write_disposition",
            memory_write.get("disposition") == expect["memory_write_disposition"],
            f"got={memory_write.get('disposition')}",
        )
    if "memory_fanout_allowed" in expect:
        add(
            "memory_fanout_allowed",
            bool(memory_write.get("fanout_allowed")) is bool(expect["memory_fanout_allowed"]),
            f"got={memory_write.get('fanout_allowed')}",
        )
    if "memory_recall_degraded" in expect:
        add(
            "memory_recall_degraded",
            bool(memory.get("degraded")) is bool(expect["memory_recall_degraded"]),
            f"got={memory.get('degraded')}",
        )
    if "memory_recall_degraded_reason" in expect:
        add(
            "memory_recall_degraded_reason",
            memory.get("degraded_reason") == expect["memory_recall_degraded_reason"],
            f"got={memory.get('degraded_reason')}",
        )
    if "memory_context_injected" in expect:
        add(
            "memory_context_injected",
            bool(memory.get("context_injected")) is bool(expect["memory_context_injected"]),
            f"got={memory.get('context_injected')}",
        )
    if "privacy_mode" in expect:
        add("privacy_mode", privacy_mode == expect["privacy_mode"], f"got={privacy_mode}")
    if "context_contains_segments" in expect:
        segments = set(context.get("segment_kinds") or [])
        for kind in expect["context_contains_segments"]:
            add(f"context_segment:{kind}", kind in segments, f"got={sorted(segments)}")
    if "context_degraded_sources" in expect:
        sources = set(context.get("degraded_sources") or [])
        for source in expect["context_degraded_sources"]:
            add(f"context_degraded_source:{source}", source in sources, f"got={sorted(sources)}")
    if "tool_names" in expect:
        names = tools.get("names") or []
        for name in expect["tool_names"]:
            add(f"tool_name:{name}", name in names, f"got={names}")
    if "forbidden_tool_names" in expect:
        names = tools.get("names") or []
        for name in expect["forbidden_tool_names"]:
            add(f"forbidden_tool:{name}", name not in names, f"got={names}")
    if "tool_error_count" in expect:
        add(
            "tool_error_count",
            int(tools.get("error_count") or 0) == int(expect["tool_error_count"]),
            f"got={tools.get('error_count')}",
        )
    if "max_tool_repeat_suppressed" in expect:
        actual = int(tools.get("repeat_suppressed_count") or 0)
        add(
            "max_tool_repeat_suppressed",
            actual <= int(expect["max_tool_repeat_suppressed"]),
            f"got={actual}",
        )
    if "context_budget_shadow_dropped_count_min" in expect:
        budget = guards.get("context_budget") or {}
        actual = int(budget.get("shadow_dropped_count") or 0)
        add(
            "context_budget_shadow_dropped_count_min",
            actual >= int(expect["context_budget_shadow_dropped_count_min"]),
            f"got={actual}",
        )
    if "memory_write_requires_consent" in expect:
        requires = memory_write.get("disposition") == "sensitive_requires_consent"
        add(
            "memory_write_requires_consent",
            requires is bool(expect["memory_write_requires_consent"]),
            f"got={memory_write.get('disposition')}",
        )
    if "required_event_kinds" in expect:
        kinds = {e["kind"] for e in events}
        for kind in expect["required_event_kinds"]:
            add(f"required_event:{kind}", kind in kinds)
    if "max_first_delta_ms" in expect:
        add(
            "max_first_delta_ms",
            first_delta_ms is not None and first_delta_ms <= int(expect["max_first_delta_ms"]),
            f"got={first_delta_ms}",
        )
    if "max_total_ms" in expect:
        add(
            "max_total_ms",
            total_ms is not None and total_ms <= int(expect["max_total_ms"]),
            f"got={total_ms}",
        )
    return checks


def _scenario_checks(
    expect: dict[str, Any], turn_reports: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    if "forbidden_assistant_substrings" in expect:
        full = "\n".join(str(t.get("assistant_preview") or "") for t in turn_reports)
        for item in expect["forbidden_assistant_substrings"]:
            checks.append(
                {
                    "name": f"scenario_forbidden_assistant:{item}",
                    "passed": item not in full,
                    "detail": "",
                }
            )
    return checks


def _startup_failure_report(
    *,
    scenarios: list[dict[str, Any]],
    http_base: str,
    grpc_target: str,
    tenant_id: str,
    user_id: str,
    error: Exception,
) -> dict[str, Any]:
    detail = f"{type(error).__name__}: {error}"
    return {
        "schema_version": "eidolon_agent.live_service_replay_report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live_service",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": None,
        "target": {
            "http_base": http_base,
            "grpc_target": grpc_target,
        },
        "passed": False,
        "summary": {
            "scenario_count": len(scenarios),
            "passed": 0,
            "failed": len(scenarios),
            "startup_error": detail,
        },
        "metrics": {
            "turn_count": 0,
            "check_count": 0,
            "check_pass_rate": 0,
            "scenario_pass_rate": 0,
            "categories": {},
            "first_delta_ms": {"p50": None, "p95": None, "max": None},
            "total_ms": {"p50": None, "p95": None, "max": None},
            "first_delta_p50_ms": None,
            "first_delta_p95_ms": None,
            "total_p50_ms": None,
            "total_p95_ms": None,
        },
        "scenarios": [
            {
                "scenario_id": str(s.get("id") or "scenario"),
                "description": str(s.get("description") or ""),
                "category": str(s.get("category") or "startup"),
                "tags": list(s.get("tags") or []),
                "passed": False,
                "turns": [],
                "checks": [
                    {
                        "name": "service_startup",
                        "passed": False,
                        "detail": detail,
                    }
                ],
            }
            for s in scenarios
        ],
    }


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    return int(statistics.median(values))


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))
    return ordered[idx]


def _merge_expectations(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    if override.get("skip_default_expect"):
        return {
            key: value
            for key, value in override.items()
            if key != "skip_default_expect"
        }
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = [*merged[key], *value]
        else:
            merged[key] = value
    return merged


def _category_metrics(scenarios: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    categories: dict[str, dict[str, int]] = {}
    for scenario in scenarios:
        category = str(scenario.get("category") or "uncategorized")
        bucket = categories.setdefault(
            category,
            {"scenario_count": 0, "passed": 0, "failed": 0, "turn_count": 0},
        )
        bucket["scenario_count"] += 1
        bucket["turn_count"] += len(scenario.get("turns") or [])
        if bool(scenario.get("passed")):
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
    return categories


def _check_count(scenarios: list[dict[str, Any]]) -> int:
    return sum(
        1
        for scenario in scenarios
        for turn in scenario.get("turns") or []
        for check in turn.get("checks") or []
        if not check.get("skipped")
    ) + sum(
        1
        for scenario in scenarios
        for check in scenario.get("checks") or []
        if not check.get("skipped")
    )


def _check_pass_rate(scenarios: list[dict[str, Any]]) -> float:
    checks = [
        check
        for scenario in scenarios
        for turn in scenario.get("turns") or []
        for check in turn.get("checks") or []
        if not check.get("skipped")
    ]
    checks.extend(
        check
        for scenario in scenarios
        for check in scenario.get("checks") or []
        if not check.get("skipped")
    )
    return _ratio(sum(1 for check in checks if bool(check.get("passed"))), len(checks))


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return round(numerator / denominator, 4)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
