from __future__ import annotations

import httpx
import pytest

from scripts import replay_live_service


def test_live_turn_checks_read_admin_observability_summary() -> None:
    checks = replay_live_service._turn_checks(
        expect={
            "memory_write_disposition": "semantic_upsert",
            "memory_fanout_allowed": True,
            "privacy_mode": "normal",
            "context_contains_segments": ["persona", "memory"],
            "tool_names": ["get_time"],
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
                "tools": {"names": ["get_time"], "error_count": 0},
                "development_guards": {},
            }
        },
        first_delta_ms=120,
        total_ms=500,
    )

    assert checks
    assert all(c["passed"] for c in checks)


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
