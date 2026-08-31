"""Live service benchmark helpers for the real gRPC/admin path."""

from __future__ import annotations

import asyncio
import os
import statistics
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import httpx
from eidolon_sdk.biz.runtime import resolve_shared_secret, sign_runtime_token

from eidolon_agent.app.admin.authority import expected_token
from eidolon_agent.app.transport.grpc.codec import struct_to_dict
from eidolon_agent.app.transport.grpc.proto import pb, pbg
from eidolon_agent.config import load_settings

DEFAULT_BENCHMARK_COMPANION_ID = "benchmark"
DEFAULT_BENCHMARK_MEMORY_REALM_ID = "default.benchmark.default"
DEFAULT_REGISTRY_HTTP = os.getenv("EIDOLON_BENCHMARK_REGISTRY_HTTP") or "http://127.0.0.1:9000/api"


async def ensure_registry_user(
    *,
    http: httpx.AsyncClient,
    registry_base: str | None,
    tenant_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Create the benchmark user through eidolon_admin when absent."""
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


async def issue_runtime_token(
    *,
    tenant_id: str,
    user_id: str,
    template_id: str,
    companion_id: str = DEFAULT_BENCHMARK_COMPANION_ID,
    memory_realm_id: str = DEFAULT_BENCHMARK_MEMORY_REALM_ID,
    device_name: str = "benchmark",
    ttl_seconds: int = 3600,
) -> tuple[str, str]:
    """Mint a real runtime token using the same secret as the running agent."""
    del tenant_id, template_id, memory_realm_id
    settings = load_settings()
    secret = resolve_shared_secret(settings.runtime_token.jwt_secret)
    if not secret:
        secret_path = Path(settings.runtime.run_dir).expanduser() / "jwt-secret"
        if not secret_path.exists():
            raise RuntimeError(
                "runtime token secret not configured and persisted jwt-secret is missing"
            )
        secret = secret_path.read_text(encoding="utf-8").strip()
    device_id = f"{device_name}-{uuid.uuid4().hex[:8]}"
    session_id = f"benchmark-{uuid.uuid4().hex}"
    token, _ = sign_runtime_token(
        secret=secret,
        algorithm=settings.runtime_token.jwt_algorithm,
        device_id=device_id,
        owner_id=user_id,
        companion_id=companion_id,
        session_id=session_id,
        scopes=["benchmark"],
        ttl_seconds=ttl_seconds,
    )
    return token, user_id


async def _issue_token(
    *,
    http: httpx.AsyncClient,
    http_base: str,
    grpc_target: str,
    tenant_id: str,
    user_id: str,
    template_id: str,
) -> tuple[str, str]:
    del http, http_base, grpc_target
    return await issue_runtime_token(
        tenant_id=tenant_id,
        user_id=user_id,
        template_id=template_id,
        device_name="live-service-benchmark",
    )


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
    turn_timeout_s: float = 45.0,
) -> dict[str, Any]:
    metadata = _authorization_metadata(token)
    scenario_reports = []
    async with grpc.aio.insecure_channel(grpc_target) as channel:
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
                    turn_timeout_s=turn_timeout_s,
                )
            )
    turns = [turn for scenario in scenario_reports for turn in scenario["turns"]]
    first = [t["first_delta_ms"] for t in turns if t.get("first_delta_ms") is not None]
    total = [t["total_ms"] for t in turns if t.get("total_ms") is not None]
    return {
        "schema_version": "eidolon_agent.live_service_replay_report.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "live_service",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": conversation_id,
        "target": {"http_base": http_base, "grpc_target": grpc_target},
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
            "categories": _category_metrics(scenario_reports),
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
    turn_timeout_s: float,
) -> dict[str, Any]:
    turn_reports = []
    for idx, turn in enumerate(scenario.get("turns") or []):
        logical_turn_id = str(turn.get("turn_id") or f"{scenario_id}-{idx + 1}")
        turn_id = f"{logical_turn_id}-{uuid.uuid4().hex[:6]}"
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
                expect=_merge_expectations(
                    scenario.get("default_turn_expect") or {},
                    turn.get("expect") or {},
                ),
                admin_delay_s=admin_delay_s,
                admin_timeout_s=admin_timeout_s,
                turn_timeout_s=turn_timeout_s,
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
    turn_timeout_s: float,
) -> dict[str, Any]:
    started = time.monotonic()
    assistant_parts: list[str] = []
    events: list[dict[str, Any]] = []
    first_delta_ms: int | None = None
    first_progress_ms: int | None = None
    first_tool_call_ms: int | None = None
    total_ms: int | None = None
    error: str | None = None

    async def _requests():
        start = pb.StartTurn(
            turn_id=turn_id,
            conversation_id=conversation_id,
            text=text,
            input_modality="text",
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

        async def _receive_until_terminal() -> None:
            nonlocal error, first_delta_ms, first_progress_ms, first_tool_call_ms, total_ms
            async for ev in call:
                if ev.turn_id != turn_id:
                    continue
                kind = pb.TurnEvent.Kind.Name(ev.kind)
                data = struct_to_dict(ev.data) if ev.data else {}
                received_ms = int((time.monotonic() - started) * 1000)
                events.append({"kind": kind, "data": data, "receive_ms": received_ms})
                if kind == "DELTA":
                    if first_delta_ms is None:
                        first_delta_ms = received_ms
                    assistant_parts.append(str(data.get("text") or ""))
                elif kind == "PROGRESS" and first_progress_ms is None:
                    first_progress_ms = received_ms
                elif kind == "TOOL_CALL" and first_tool_call_ms is None:
                    first_tool_call_ms = received_ms
                elif kind == "DONE":
                    total_ms = int((time.monotonic() - started) * 1000)
                    break
                elif kind == "ERROR":
                    total_ms = int((time.monotonic() - started) * 1000)
                    error = str(data.get("message") or data.get("code") or "error")
                    break

        await asyncio.wait_for(_receive_until_terminal(), timeout=turn_timeout_s)
    except TimeoutError:
        total_ms = int((time.monotonic() - started) * 1000)
        error = f"turn_timeout after {turn_timeout_s:g}s"
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
        "first_progress_ms": first_progress_ms,
        "first_tool_call_ms": first_tool_call_ms,
        "first_model_activity_ms": min(
            value
            for value in (first_progress_ms, first_tool_call_ms, first_delta_ms)
            if value is not None
        )
        if any(
            value is not None for value in (first_progress_ms, first_tool_call_ms, first_delta_ms)
        )
        else None,
        "output_path": _classify_output_path(
            first_progress_ms=first_progress_ms,
            first_tool_call_ms=first_tool_call_ms,
            first_delta_ms=first_delta_ms,
            error=error,
        ),
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


def _classify_output_path(
    *,
    first_progress_ms: int | None,
    first_tool_call_ms: int | None,
    first_delta_ms: int | None,
    error: str | None,
) -> str:
    """Classify slow output without conflating activity and playable text."""

    if first_delta_ms is not None:
        if first_progress_ms is not None and first_progress_ms <= first_delta_ms:
            return "visible_after_progress"
        if first_tool_call_ms is not None and first_tool_call_ms <= first_delta_ms:
            return "visible_after_tool"
        return "visible_direct"
    if first_progress_ms is not None:
        return "progress_without_visible_output"
    if first_tool_call_ms is not None:
        return "tool_without_visible_output"
    if error:
        return "silent_error"
    return "empty_completion"


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
                # The admin surface requires the Host's credential. Read from the
                # environment rather than added to this tool's arguments: the
                # operator running a live benchmark already has the Host's
                # ``agent.env`` in front of them, and a flag would be a second
                # place to keep the same secret.
                headers={"Authorization": f"Bearer {expected_token()}"},
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
    transient = (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadError,
        httpx.ReadTimeout,
    )
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
    expect: dict[str, Any],
    turn_reports: list[dict[str, Any]],
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
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "live_service",
        "tenant_id": tenant_id,
        "user_id": user_id,
        "conversation_id": None,
        "target": {"http_base": http_base, "grpc_target": grpc_target},
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


def _merge_expectations(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    if override.get("skip_default_expect"):
        return {key: value for key, value in override.items() if key != "skip_default_expect"}
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


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return round(numerator / denominator, 4)


def _authorization_metadata(token: str) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {token}"),)
