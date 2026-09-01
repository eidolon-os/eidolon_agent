from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import pytest
from eidolon_memory_contracts import unwrap_memory_payload

from eidolon_agent.app.benchmark import live_local_contract
from eidolon_agent.app.benchmark.live_local_contract import (
    LiveLocalContractConfig,
    run_live_local_contract,
)
from eidolon_agent.core.errors import MemoryUnavailableError, NatsUnavailableError

pytestmark = pytest.mark.integration


def test_live_local_contract_rejects_passing_dependency_policy() -> None:
    with pytest.raises(ValueError, match="dependency_unavailable_status"):
        LiveLocalContractConfig(dependency_unavailable_status="passed")  # type: ignore[arg-type]


async def test_http_json_check_retries_transient_connect_failure(monkeypatch) -> None:
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        await real_sleep(0)

    class _Response:
        status_code = 200
        text = '{"status":"ready"}'

        def json(self):
            return {"status": "ready"}

    class _FlakyClient:
        attempts = 0

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str):
            assert url == "http://127.0.0.1:8180/readyz"
            _FlakyClient.attempts += 1
            if _FlakyClient.attempts == 1:
                raise OSError("connection refused")
            return _Response()

    monkeypatch.setattr(live_local_contract.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(live_local_contract.httpx, "AsyncClient", _FlakyClient)

    check = await live_local_contract._http_json_check(
        name="agent_http_readyz",
        url="http://127.0.0.1:8180/readyz",
        required=True,
        timeout_s=0.1,
        expected_status=200,
        expected_json={"status": "ready"},
        unavailable_status="failed",
        hint="start agent",
    )

    assert check.status == "passed"
    assert check.details["attempts"] == 2


class _FakeRoutes:
    async def memory_space_ids(self) -> list[str]:
        return ["r_contract"]

    async def source(self) -> str:
        return "discovery"

    async def render_turn_subject(self, memory_space_id: str) -> str:
        return f"eidolon.memory.turn.{memory_space_id}"


class _FakeAgentSession:
    def __init__(self) -> None:
        self.search_calls = 0

    async def tool_names(self):
        return frozenset(
            {
                "eidolon_memory_active_commitments",
                "eidolon_memory_recall_context",
                "eidolon_memory_search",
            }
        )

    async def call_tool(self, name, arguments):
        assert name == "eidolon_memory_search"
        self.search_calls += 1
        if self.search_calls == 1:
            return []
        assert arguments["query"].startswith("海棠播客")
        published = unwrap_memory_payload(_FakeBus.published[-1][0].payload)
        source_event_id = published["turn_id"]
        return [
            {
                "key": "drawer-1",
                "value": "我最近开始追一档播客，名字叫海棠播客。",
                "metadata": {"source_event_id": source_event_id},
            }
        ]


class _FakeOpsSession:
    async def tool_names(self):
        return frozenset(
            {
                "eidolon_memory_status",
                "eidolon_memory_get_by_source_turn",
                "eidolon_memory_forget_source_event",
            }
        )

    async def call_tool(self, name, arguments):
        if name == "eidolon_memory_status":
            return {
                "ready": True,
                "memory_space_id": "r_contract",
                "mcp_transport": "streamable-http",
            }
        raise AssertionError(f"unexpected MCP tool {name}")


class _FakePool:
    closed = False

    def __init__(self, *, routes) -> None:
        self.routes = routes
        self.agent_session = _FakeAgentSession()
        self.ops_session = _FakeOpsSession()

    async def session_for(self, memory_space_id: str):
        assert memory_space_id == "r_contract"
        return self.agent_session

    async def write_session_for(self, memory_space_id: str):
        assert memory_space_id == "r_contract"
        return self.ops_session

    async def close_all(self) -> None:
        self.closed = True


class _FakeBus:
    closed = False
    published: ClassVar[list[tuple[object, bool]]] = []

    def __init__(self, url: str, *, creds_path=None) -> None:
        self.url = url
        self.creds_path = creds_path

    async def close(self) -> None:
        self.closed = True

    async def publish(self, event, *, persistent=False) -> None:
        self.published.append((event, persistent))


class _StaticSessionPool:
    def __init__(self, session) -> None:
        self.session = session
        self.drops = 0

    async def session_for(self, memory_space_id: str):
        assert memory_space_id == "r_contract"
        return self.session

    async def write_session_for(self, memory_space_id: str):
        assert memory_space_id == "r_contract"
        return self.session

    async def drop_session(self, memory_space_id: str, *, session=None) -> bool:
        assert memory_space_id == "r_contract"
        assert session is self.session
        self.drops += 1
        return True

    async def drop_write_session(self, memory_space_id: str, *, session=None) -> bool:
        assert memory_space_id == "r_contract"
        assert session is self.session
        self.drops += 1
        return True


async def test_live_local_contract_memory_publish_and_readback(monkeypatch) -> None:
    async def fake_build_initial_memory_routes(
        *,
        memory,
        nats,
        log_initial_fetch_exception=True,
    ):
        del memory, nats, log_initial_fetch_exception
        return _FakeRoutes(), "nats://test:4222", None

    monkeypatch.setattr(
        live_local_contract,
        "load_settings",
        lambda: SimpleNamespace(memory=object(), nats=SimpleNamespace(creds_path=None)),
    )
    monkeypatch.setattr(
        live_local_contract,
        "build_initial_memory_routes",
        fake_build_initial_memory_routes,
    )
    monkeypatch.setattr(live_local_contract, "McpClientPool", _FakePool)
    monkeypatch.setattr(live_local_contract, "NatsEventBus", _FakeBus)
    _FakeBus.published.clear()

    report = await run_live_local_contract(
        LiveLocalContractConfig(
            include_agent_http=False,
            include_agent_admin=False,
            include_admin_gateway=False,
            include_memory=True,
            require_memory_readback=True,
            memory_owner_id="live-local-contract",
            memory_companion_id="live-local-contract",
            require_memory_cleanup=False,
            memory_readback_poll_s=0.001,
        )
    )

    assert report.passed is True
    assert [check.name for check in report.checks] == [
        "memory_discovery",
        "memory_agent_mcp_tools",
        "memory_ops_mcp_tools",
        "memory_mcp_status",
        "memory_nats_publish",
        "memory_nats_readback",
        "memory_marker_cleanup",
    ]
    assert all(check.status == "passed" for check in report.checks[:-1])
    assert report.checks[-1].status == "skipped"
    assert _FakeBus.published
    event, persistent = _FakeBus.published[-1]
    assert persistent is True
    published = unwrap_memory_payload(event.payload)
    assert published["context"]["memory_realm_id"] == "r_contract"
    assert published["user_text"].startswith("我最近开始追一档播客，名字叫海棠播客")
    assert "请记住" not in published["user_text"]
    assert published["metadata"] == {
        "source": "eidolon-agent-live-local-contract",
        "source_project": "eidolon_agent",
        "source_component": "history.fanout",
        "source_turn_id": published["turn_id"],
        "owner_id": "live-local-contract",
        "companion_id": "live-local-contract",
        "memory_realm_id": "r_contract",
        "purpose": "memory-contract-readback",
    }


async def test_live_local_contract_readback_retries_transient_timeout() -> None:
    class TimeoutThenRecordSession:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_search"
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError()
            return {
                "records": [
                    {
                        "key": "drawer-timeout-retry",
                        "value": "我最近开始追一档播客，名字叫海棠播客。",
                        "metadata": {"source_event_id": "turn-timeout-retry"},
                    }
                ]
            }

    session = TimeoutThenRecordSession()
    pool = _StaticSessionPool(session)
    check = await live_local_contract._memory_readback_check(
        pool=pool,
        tool_names={"eidolon_memory_search"},
        memory_space_id="r_contract",
        marker="turn-timeout-retry",
        owner_id="owner-contract",
        companion_id="companion-contract",
        cfg=LiveLocalContractConfig(
            memory_readback_timeout_s=0.1,
            memory_readback_poll_s=0.001,
            timeout_s=0.01,
        ),
    )

    assert check.status == "passed"
    assert session.calls == 2
    assert pool.drops == 1
    assert check.details["record_key"] == "drawer-timeout-retry"


async def test_live_local_contract_readback_retries_cancelled_mcp_call() -> None:
    class CancelThenRecordSession:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_search"
            self.calls += 1
            if self.calls == 1:
                raise asyncio.CancelledError("cancel scope closed")
            return [
                {
                    "key": "drawer-cancel-retry",
                    "value": "我最喜欢的播客是 Acquired 半导体播客。",
                    "metadata": {"source_event_id": "turn-cancel-retry"},
                }
            ]

    session = CancelThenRecordSession()
    pool = _StaticSessionPool(session)
    check = await live_local_contract._memory_readback_check(
        pool=pool,
        tool_names={"eidolon_memory_search"},
        memory_space_id="r_contract",
        marker="turn-cancel-retry",
        owner_id="owner-contract",
        companion_id="companion-contract",
        cfg=LiveLocalContractConfig(
            memory_readback_timeout_s=0.1,
            memory_readback_poll_s=0.001,
            timeout_s=0.01,
        ),
    )

    assert check.status == "passed"
    assert session.calls == 2
    assert pool.drops == 1
    assert check.details["record_key"] == "drawer-cancel-retry"


async def test_call_mcp_tool_wait_cancellation_is_timeout(monkeypatch) -> None:
    class SlowSession:
        async def call_tool(self, name, arguments):
            del name, arguments
            await asyncio.sleep(60)
            return {"record": {"key": "late"}}

    async def cancelled_wait(tasks, timeout):
        del tasks, timeout
        raise asyncio.CancelledError("cancel scope closed")

    monkeypatch.setattr(live_local_contract.asyncio, "wait", cancelled_wait)

    with pytest.raises(asyncio.TimeoutError):
        await live_local_contract._call_mcp_tool_with_timeout(
            SlowSession(),
            "eidolon_memory_search",
            {"query": "turn-cancel-wait"},
            timeout_s=0.01,
        )


async def test_live_local_contract_readback_child_timeout_does_not_leak_cancel() -> None:
    class HangingSession:
        def __init__(self) -> None:
            self.calls = 0
            self.cancelled = 0

        async def call_tool(self, name, arguments):
            del name, arguments
            self.calls += 1
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise

    session = HangingSession()
    pool = _StaticSessionPool(session)
    check = await live_local_contract._memory_readback_check(
        pool=pool,
        tool_names={"eidolon_memory_search"},
        memory_space_id="r_contract",
        marker="turn-child-timeout",
        owner_id="owner-contract",
        companion_id="companion-contract",
        cfg=LiveLocalContractConfig(
            memory_readback_timeout_s=0.01,
            memory_readback_poll_s=0.001,
            timeout_s=0.001,
        ),
    )

    assert check.status == "failed"
    assert check.summary == "timed out waiting for memory readback"
    assert check.details["last_error"].startswith("TimeoutError:")
    assert session.calls >= 1
    assert session.cancelled == session.calls
    assert pool.drops == session.calls


async def test_live_local_contract_readback_unavailable_uses_dependency_policy() -> None:
    class UnavailableSession:
        async def call_tool(self, name, arguments):
            del name, arguments
            raise MemoryUnavailableError("mcp warming")

    check = await live_local_contract._memory_readback_check(
        pool=_StaticSessionPool(UnavailableSession()),
        tool_names={"eidolon_memory_search"},
        memory_space_id="r_contract",
        marker="turn-unavailable",
        owner_id="owner-contract",
        companion_id="companion-contract",
        cfg=LiveLocalContractConfig(
            dependency_unavailable_status="skipped",
            memory_readback_timeout_s=0.01,
            memory_readback_poll_s=0.001,
            timeout_s=0.001,
        ),
    )

    assert check.status == "skipped"
    assert check.required is True
    assert check.summary == "timed out waiting for memory readback"
    assert check.details["last_error"] == "mcp warming"


async def test_memory_scope_isolation_uses_source_identity_not_result_wording() -> None:
    class ScopedSession:
        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_search"
            companion_id = arguments["context"]["companion_id"]
            if companion_id == "companion-primary":
                return {
                    "records": [
                        {
                            "key": "drawer-primary",
                            "value": "paraphrased by the steward",
                            "metadata": {"source_event_id": "turn-scope"},
                        }
                    ]
                }
            return {
                "records": [
                    {
                        "key": f"unrelated-{companion_id}",
                        "value": "same search can return unrelated memories",
                        "metadata": {"source_event_id": "another-turn"},
                    }
                ]
            }

    check = await live_local_contract._memory_scope_isolation_check(
        pool=_StaticSessionPool(ScopedSession()),
        memory_space_id="r_contract",
        marker="turn-scope",
        owner_id="owner-contract",
        primary_companion_id="companion-primary",
        isolation_companion_ids=("companion-a", "companion-b"),
        cfg=LiveLocalContractConfig(timeout_s=0.1),
    )

    assert check.status == "passed"
    assert check.details["leaked_to"] == []
    assert check.details["results"]["companion-primary"]["matching_source_events"] == 1
    assert check.details["results"]["companion-a"]["matching_source_events"] == 0
    assert check.details["results"]["companion-b"]["matching_source_events"] == 0


async def test_memory_scope_isolation_reports_cross_companion_leak() -> None:
    class LeakingSession:
        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_search"
            return {
                "records": [
                    {
                        "key": arguments["context"]["companion_id"],
                        "value": "leaked",
                        "metadata": {"source_event_id": "turn-scope"},
                    }
                ]
            }

    check = await live_local_contract._memory_scope_isolation_check(
        pool=_StaticSessionPool(LeakingSession()),
        memory_space_id="r_contract",
        marker="turn-scope",
        owner_id="owner-contract",
        primary_companion_id="companion-primary",
        isolation_companion_ids=("companion-a", "companion-b"),
        cfg=LiveLocalContractConfig(timeout_s=0.1),
    )

    assert check.status == "failed"
    assert check.details["leaked_to"] == ["companion-a", "companion-b"]


async def test_memory_recall_latency_fails_when_source_disappears() -> None:
    class AlternatingSession:
        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, name, arguments):
            del name, arguments
            self.calls += 1
            if self.calls == 2:
                return {"records": []}
            return {
                "records": [
                    {
                        "key": "drawer-latency",
                        "value": "visible",
                        "metadata": {"source_event_id": "turn-latency"},
                    }
                ]
            }

    check = await live_local_contract._memory_recall_latency_check(
        pool=_StaticSessionPool(AlternatingSession()),
        memory_space_id="r_contract",
        marker="turn-latency",
        owner_id="owner-contract",
        companion_id="companion-primary",
        cfg=LiveLocalContractConfig(memory_recall_samples=3, timeout_s=0.1),
    )

    assert check.status == "failed"
    assert check.details["samples"] == 3
    assert check.details["missing_source_event_samples"] == 1


async def test_memory_cleanup_uses_exact_source_event_and_proves_absence() -> None:
    class _OpsSession:
        calls: ClassVar[list[tuple[str, dict]]] = []

        async def call_tool(self, name, arguments):
            self.calls.append((name, dict(arguments)))
            return {
                "status": "applied",
                "request_id": "cleanup-command",
                "source_event_id": arguments["source_event_id"],
                "source_event_tombstoned": True,
            }

    class _GoneSession:
        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_search"
            assert arguments["query"] == "海棠播客canary是什么？"
            assert arguments["context"]["owner_id"] == "owner_contract"
            assert arguments["context"]["companion_id"] == "companion_contract"
            return []

    ops = _OpsSession()
    check = await live_local_contract._memory_cleanup_check(
        pool=_StaticSessionPool(_GoneSession()),
        ops_session=ops,
        memory_space_id="r_contract",
        owner_id="owner_contract",
        companion_id="companion_contract",
        marker="live-local-contract-canary",
        cfg=LiveLocalContractConfig(memory_readback_poll_s=0.001),
    )

    assert check.status == "passed"
    assert check.summary == "source event tombstoned and no longer readable"
    assert ops.calls == [
        (
            "eidolon_memory_forget_source_event",
            {"source_event_id": "live-local-contract-canary", "wait_applied_seconds": 10.0},
        )
    ]
    assert check.details["cleanup_request_id"] == "cleanup-command"
    assert check.details["source_event_tombstoned"] is True


async def test_memory_cleanup_retries_the_same_source_event_until_tombstoned() -> None:
    class _OpsSession:
        calls = 0

        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_forget_source_event"
            assert arguments["source_event_id"] == "turn-canary"
            self.calls += 1
            if self.calls == 1:
                return {
                    "status": "accepted",
                    "request_id": "cleanup-pending",
                    "source_event_tombstoned": False,
                }
            return {
                "status": "applied",
                "request_id": "cleanup-retry",
                "source_event_tombstoned": True,
            }

    class _GoneSession:
        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_search"
            return []

    ops = _OpsSession()
    check = await live_local_contract._memory_cleanup_check(
        pool=_StaticSessionPool(_GoneSession()),
        ops_session=ops,
        memory_space_id="r_contract",
        owner_id="owner_contract",
        companion_id="companion_contract",
        marker="turn-canary",
        cfg=LiveLocalContractConfig(
            memory_readback_timeout_s=0.1,
            memory_readback_poll_s=0.001,
            timeout_s=0.01,
        ),
    )

    assert check.status == "passed"
    assert ops.calls == 2
    assert check.details["cleanup_request_id"] == "cleanup-retry"


async def test_live_memory_contract_requires_explicit_scope_identity() -> None:
    report = await run_live_local_contract(
        LiveLocalContractConfig(
            include_agent_http=False,
            include_agent_admin=False,
            include_admin_gateway=False,
            include_memory=True,
        )
    )

    assert report.passed is False
    assert report.checks[0].name == "memory_test_identity"
    assert report.checks[0].status == "failed"


async def test_live_local_contract_dependency_unavailable_can_skip(monkeypatch) -> None:
    async def fake_build_initial_memory_routes(
        *,
        memory,
        nats,
        log_initial_fetch_exception=True,
    ):
        del memory, nats, log_initial_fetch_exception
        return _FakeRoutesNoIds(), "nats://test:4222", None

    class _FakeRoutesNoIds:
        async def memory_space_ids(self) -> list[str]:
            return []

        async def source(self) -> str:
            return "discovery"

    monkeypatch.setattr(
        live_local_contract,
        "load_settings",
        lambda: SimpleNamespace(memory=object(), nats=SimpleNamespace(creds_path=None)),
    )
    monkeypatch.setattr(
        live_local_contract,
        "build_initial_memory_routes",
        fake_build_initial_memory_routes,
    )

    report = await run_live_local_contract(
        LiveLocalContractConfig(
            include_agent_http=False,
            include_agent_admin=False,
            include_admin_gateway=False,
            include_memory=True,
            memory_owner_id="live-local-contract",
            memory_companion_id="live-local-contract",
            dependency_unavailable_status="skipped",
        )
    )

    assert report.passed is False
    assert report.summary["skipped_required"] == ["memory_discovery"]
    assert report.checks[0].status == "skipped"


async def test_live_local_contract_nats_unavailable_uses_dependency_policy(
    monkeypatch,
) -> None:
    class UnavailableBus(_FakeBus):
        async def publish(self, event, *, persistent=False) -> None:
            del event, persistent
            raise NatsUnavailableError("nats down")

    async def fake_build_initial_memory_routes(
        *,
        memory,
        nats,
        log_initial_fetch_exception=True,
    ):
        del memory, nats, log_initial_fetch_exception
        return _FakeRoutes(), "nats://test:4222", None

    monkeypatch.setattr(
        live_local_contract,
        "load_settings",
        lambda: SimpleNamespace(memory=object(), nats=SimpleNamespace(creds_path=None)),
    )
    monkeypatch.setattr(
        live_local_contract,
        "build_initial_memory_routes",
        fake_build_initial_memory_routes,
    )
    monkeypatch.setattr(live_local_contract, "McpClientPool", _FakePool)
    monkeypatch.setattr(live_local_contract, "NatsEventBus", UnavailableBus)

    report = await run_live_local_contract(
        LiveLocalContractConfig(
            include_agent_http=False,
            include_agent_admin=False,
            include_admin_gateway=False,
            include_memory=True,
            memory_owner_id="live-local-contract",
            memory_companion_id="live-local-contract",
            dependency_unavailable_status="skipped",
            require_memory_cleanup=False,
        )
    )

    by_name = {check.name: check for check in report.checks}
    assert by_name["memory_nats_publish"].status == "skipped"
    assert report.summary["skipped_required"] == ["memory_nats_publish"]
    assert report.passed is False


async def test_readback_that_lands_too_late_fails_instead_of_passing_quietly() -> None:
    """A budget the check declines to enforce is a number in a report, not a gate.

    The readback timeout only says when to give up waiting. Everything under it
    reported ``passed`` identically, which is how a background write that
    slowed from 18s to 59s crossed a release without failing anything.
    """

    class SlowSession:
        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_search"
            await asyncio.sleep(0.05)
            return {
                "records": [
                    {
                        "key": "drawer-slow",
                        "value": "我最近开始追一档播客，名字叫海棠播客。",
                        "metadata": {"source_event_id": "turn-slow"},
                    }
                ]
            }

    check = await live_local_contract._memory_readback_check(
        pool=_StaticSessionPool(SlowSession()),
        tool_names={"eidolon_memory_search"},
        memory_space_id="r_contract",
        marker="turn-slow",
        owner_id="owner-contract",
        companion_id="companion-contract",
        cfg=LiveLocalContractConfig(
            memory_readback_timeout_s=5.0,
            memory_readback_poll_s=0.001,
            memory_materialization_budget_s=0.01,
        ),
    )

    assert check.status == "failed"
    assert check.summary == "materialization exceeded budget"
    # The write did land — the failure is about when, so the evidence that it
    # landed has to survive into the report.
    assert check.details["record_key"] == "drawer-slow"
    assert check.details["materialization_s"] > 0.01


async def test_readback_reports_how_long_materialization_took_even_when_fast() -> None:
    """Reported on the passing path too, or a trend has no data points."""

    class FastSession:
        async def call_tool(self, name, arguments):
            return {
                "records": [
                    {
                        "key": "drawer-fast",
                        "value": "我最近开始追一档播客，名字叫海棠播客。",
                        "metadata": {"source_event_id": "turn-fast"},
                    }
                ]
            }

    check = await live_local_contract._memory_readback_check(
        pool=_StaticSessionPool(FastSession()),
        tool_names={"eidolon_memory_search"},
        memory_space_id="r_contract",
        marker="turn-fast",
        owner_id="owner-contract",
        companion_id="companion-contract",
        cfg=LiveLocalContractConfig(
            memory_readback_timeout_s=5.0,
            memory_readback_poll_s=0.001,
            memory_materialization_budget_s=10.0,
        ),
    )

    assert check.status == "passed"
    assert check.details["materialization_budget_s"] == 10.0
    assert check.details["materialization_s"] >= 0.0
