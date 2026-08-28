from __future__ import annotations

import httpx
import pytest

from eidolon_agent.app.benchmark import live_service as replay_live_service
from eidolon_agent.app.benchmark.suites import live_agent_memory_experience_scenarios


def test_live_turn_checks_read_admin_observability_summary() -> None:
    checks = replay_live_service._turn_checks(
        expect={
            "memory_write_disposition": "semantic_upsert",
            "memory_fanout_allowed": True,
            "privacy_mode": "normal",
            "context_contains_segments": ["persona", "memory"],
            "tool_names": ["delegate_to_coworker"],
            "tool_error_count": 0,
            "required_event_kinds": ["TOOL_CALL", "TOOL_RESULT", "DONE"],
            "max_first_delta_ms": 300,
            "max_total_ms": 1000,
        },
        assistant_text="小满，现在是下午。",
        events=[
            {"kind": "TOOL_CALL"},
            {"kind": "TOOL_RESULT"},
            {"kind": "DONE"},
        ],
        detail={
            "observability_summary": {
                "privacy_mode": "normal",
                "context": {"segment_kinds": ["persona", "memory", "current_user"]},
                "memory_write": {
                    "disposition": "semantic_upsert",
                    "fanout_allowed": True,
                },
                "memory": {"degraded": False},
                "tools": {"names": ["delegate_to_coworker"], "error_count": 0},
                "development_guards": {},
            }
        },
        first_delta_ms=120,
        total_ms=500,
    )

    assert checks
    assert all(c["passed"] for c in checks)


def test_live_agent_memory_benchmark_is_broad() -> None:
    scenarios = live_agent_memory_experience_scenarios()

    assert len(scenarios) >= 100
    assert {scenario["category"] for scenario in scenarios} >= {
        "context_authority",
        "memory_use",
        "memory_update",
        "memory_privacy",
        "agent_tool_control",
        "interrupt_realtime",
    }
    assert all("default_turn_expect" in scenario for scenario in scenarios)


def test_live_turn_checks_include_context_tags_and_tool_guards() -> None:
    checks = replay_live_service._turn_checks(
        expect={
            "current_request_authority": True,
            "background_non_actionable": True,
            "context_structure_version": "context_structure.v2",
            "history_presentation": "background_context",
            "no_tool_calls": True,
            "forbidden_tool_names": ["get_weather"],
            "max_tool_repeat_suppressed": 0,
            "memory_context_injected": True,
        },
        assistant_text="AMB-LIVE-001",
        events=[{"kind": "DONE"}],
        detail={
            "observability_summary": {
                "context_structure_version": "context_structure.v2",
                "history_presentation": "background_context",
                "context_tags": [
                    {
                        "kind": "current_user",
                        "authority": "current_request",
                        "actionability": "may_execute",
                    },
                    {
                        "kind": "history",
                        "authority": "background",
                        "actionability": "must_not_execute",
                    },
                ],
                "memory": {"context_injected": True},
                "tools": {"names": [], "repeat_suppressed_count": 0},
            }
        },
        first_delta_ms=10,
        total_ms=20,
    )

    assert all(c["passed"] for c in checks)


def test_live_default_expectations_can_be_merged_or_skipped() -> None:
    assert replay_live_service._merge_expectations(
        {"context_structure_version": "context_structure.v2", "required_event_kinds": ["DONE"]},
        {"required_event_kinds": ["TOOL_CALL"]},
    ) == {
        "context_structure_version": "context_structure.v2",
        "required_event_kinds": ["DONE", "TOOL_CALL"],
    }
    assert replay_live_service._merge_expectations(
        {"context_structure_version": "context_structure.v2"},
        {"skip_default_expect": True, "required_event_kinds": ["DONE"]},
    ) == {"required_event_kinds": ["DONE"]}


def test_live_report_category_metrics() -> None:
    scenarios = [
        {
            "category": "context_authority",
            "passed": True,
            "turns": [{"checks": [{"passed": True}]}],
            "checks": [],
        },
        {
            "category": "context_authority",
            "passed": False,
            "turns": [{"checks": [{"passed": False}]}],
            "checks": [],
        },
    ]

    assert replay_live_service._category_metrics(scenarios) == {
        "context_authority": {
            "scenario_count": 2,
            "passed": 1,
            "failed": 1,
            "turn_count": 2,
        }
    }
    assert replay_live_service._check_count(scenarios) == 2
    assert replay_live_service._check_pass_rate(scenarios) == 0.5


@pytest.mark.parametrize(
    ("progress", "tool", "visible", "error", "expected"),
    [
        (120, None, 12_000, None, "visible_after_progress"),
        (None, 300, 20_000, None, "visible_after_tool"),
        (None, None, 500, None, "visible_direct"),
        (120, None, None, "provider failed", "progress_without_visible_output"),
        (None, None, None, "provider silent", "silent_error"),
        (None, None, None, None, "empty_completion"),
    ],
)
def test_output_path_latency_matrix_classification(
    progress: int | None,
    tool: int | None,
    visible: int | None,
    error: str | None,
    expected: str,
) -> None:
    assert replay_live_service._classify_output_path(
        first_progress_ms=progress,
        first_tool_call_ms=tool,
        first_delta_ms=visible,
        error=error,
    ) == expected


def test_live_turn_checks_detects_missing_admin_trace() -> None:
    checks = replay_live_service._turn_checks(
        expect={},
        assistant_text="",
        events=[{"kind": "DONE"}],
        detail={"error": "HTTP 404", "observability_summary": None},
        first_delta_ms=None,
        total_ms=10,
    )

    by_name = {c["name"]: c for c in checks}
    assert by_name["stream_done"]["passed"] is True
    assert by_name["admin_trace_available"]["passed"] is False
    assert by_name["admin_trace_available"]["detail"] == "HTTP 404"


def test_live_turn_checks_accepts_sensitive_requires_consent() -> None:
    checks = replay_live_service._turn_checks(
        expect={
            "memory_write_disposition": "sensitive_requires_consent",
            "memory_write_requires_consent": True,
            "memory_fanout_allowed": False,
        },
        assistant_text="",
        events=[{"kind": "DONE"}],
        detail={
            "observability_summary": {
                "memory_write": {
                    "disposition": "sensitive_requires_consent",
                    "fanout_allowed": False,
                }
            }
        },
        first_delta_ms=None,
        total_ms=10,
    )

    by_name = {c["name"]: c for c in checks}
    assert by_name["memory_write_requires_consent"]["passed"] is True
    assert by_name["memory_fanout_allowed"]["passed"] is True


def test_live_turn_checks_can_assert_memory_degraded_reason() -> None:
    checks = replay_live_service._turn_checks(
        expect={
            "memory_recall_degraded": True,
            "memory_recall_degraded_reason": "no_memory_route",
        },
        assistant_text="",
        events=[{"kind": "DONE"}],
        detail={
            "observability_summary": {
                "memory": {
                    "degraded": True,
                    "degraded_reason": "no_memory_route",
                }
            }
        },
        first_delta_ms=None,
        total_ms=10,
    )

    by_name = {c["name"]: c for c in checks}
    assert by_name["memory_recall_degraded"]["passed"] is True
    assert by_name["memory_recall_degraded_reason"]["passed"] is True


def test_startup_failure_report_is_readable_and_structured() -> None:
    report = replay_live_service._startup_failure_report(
        scenarios=[{"id": "s1", "description": "desc"}],
        http_base="http://127.0.0.1:8081",
        grpc_target="127.0.0.1:45051",
        tenant_id="demo",
        user_id="u1",
        error=RuntimeError("service down"),
    )

    assert report["passed"] is False
    assert report["summary"]["startup_error"] == "RuntimeError: service down"
    assert report["scenarios"][0]["checks"][0]["name"] == "service_startup"


@pytest.mark.asyncio
async def test_fetch_turn_detail_polls_until_admin_trace_is_available() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(
            200,
            json={"status": "completed", "observability_summary": {"privacy_mode": "normal"}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        detail = await replay_live_service._fetch_turn_detail(
            http=client,
            http_base="http://agent.test",
            turn_id="t1",
            timeout_s=1.0,
        )

    assert attempts == 2
    assert detail["status"] == "completed"


@pytest.mark.asyncio
async def test_fetch_turn_detail_returns_stable_error_after_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        detail = await replay_live_service._fetch_turn_detail(
            http=client,
            http_base="http://agent.test",
            turn_id="missing",
            timeout_s=0.0,
        )

    assert detail["status"] == "admin_detail_unavailable"
    assert detail["observability_summary"] is None
    assert detail["error"] == "HTTP 404"


@pytest.mark.asyncio
async def test_ensure_registry_user_returns_existing_user() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/users/alice"
        return httpx.Response(
            200,
            json={
                "health": {"worker_running": True},
                "mcp_http_url": "http://127.0.0.1:8031/mcp",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await replay_live_service.ensure_registry_user(
            http=client,
            registry_base="http://admin.test/api",
            tenant_id="demo",
            user_id="alice",
        )

    assert result == {
        "status": "exists",
        "user_id": "alice",
        "health": {"worker_running": True},
        "mcp_http_url": "http://127.0.0.1:8031/mcp",
    }


@pytest.mark.asyncio
async def test_ensure_registry_user_creates_missing_user() -> None:
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "missing"})
        assert request.method == "POST"
        assert request.url.path == "/api/users"
        assert request.content
        return httpx.Response(
            201,
            json={
                "health": {"worker_running": True},
                "mcp_http_url": "http://127.0.0.1:8032/mcp",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await replay_live_service.ensure_registry_user(
            http=client,
            registry_base="http://admin.test/api",
            tenant_id="demo",
            user_id="new-user",
        )

    assert calls == [("GET", "/api/users/new-user"), ("POST", "/api/users")]
    assert result["status"] == "created"
    assert result["mcp_http_url"] == "http://127.0.0.1:8032/mcp"
