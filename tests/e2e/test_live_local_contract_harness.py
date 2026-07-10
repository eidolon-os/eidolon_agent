from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

from eidolon_agent.app.benchmark import live_local_contract
from eidolon_agent.app.benchmark.live_local_contract import (
    LiveLocalContractConfig,
    run_live_local_contract,
)
from eidolon_agent.core.errors import NatsUnavailableError

pytestmark = pytest.mark.integration


def test_live_local_contract_rejects_passing_dependency_policy() -> None:
    with pytest.raises(ValueError, match="dependency_unavailable_status"):
        LiveLocalContractConfig(dependency_unavailable_status="passed")  # type: ignore[arg-type]


class _FakeRoutes:
    async def memory_space_ids(self) -> list[str]:
        return ["r_contract"]

    async def source(self) -> str:
        return "discovery"

    async def render_turn_subject(self, memory_space_id: str) -> str:
        return f"eidolon.memory.turn.{memory_space_id}"


class _FakeSession:
    def __init__(self) -> None:
        self.readback_calls = 0

    async def tool_names(self):
        return frozenset(
            {
                "eidolon_memory_status",
                "eidolon_memory_recall_context",
                "eidolon_memory_search",
                "eidolon_memory_get_by_source_turn",
            }
        )

    async def call_tool(self, name, arguments):
        if name == "eidolon_memory_status":
            return {
                "ready": True,
                "memory_space_id": "r_contract",
                "mcp_transport": "streamable-http",
            }
        if name == "eidolon_memory_get_by_source_turn":
            self.readback_calls += 1
            if self.readback_calls == 1:
                return {"record": None}
            return {
                "record": {
                    "key": "drawer-1",
                    "metadata": {"source_turn_id": arguments["source_turn_id"]},
                }
            }
        raise AssertionError(f"unexpected MCP tool {name}")


class _FakePool:
    closed = False

    def __init__(self, *, routes) -> None:
        self.routes = routes
        self.session = _FakeSession()

    async def session_for(self, memory_space_id: str):
        assert memory_space_id == "r_contract"
        return self.session

    async def close_all(self) -> None:
        self.closed = True


class _FakeBus:
    closed = False

    def __init__(self, url: str, *, creds_path=None) -> None:
        self.url = url
        self.creds_path = creds_path

    async def close(self) -> None:
        self.closed = True


class _FakePublisher:
    published: ClassVar[list[dict]] = []

    def __init__(self, *, event_bus, routes) -> None:
        self.event_bus = event_bus
        self.routes = routes

    async def publish_turn(self, **kwargs) -> None:
        self.published.append(kwargs)


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
    monkeypatch.setattr(live_local_contract, "MemoryNatsPublisher", _FakePublisher)

    report = await run_live_local_contract(
        LiveLocalContractConfig(
            include_agent_http=False,
            include_agent_admin=False,
            include_admin_gateway=False,
            include_memory=True,
            require_memory_readback=True,
            memory_readback_poll_s=0.001,
        )
    )

    assert report.passed is True
    assert [check.name for check in report.checks] == [
        "memory_discovery",
        "memory_mcp_tools",
        "memory_mcp_status",
        "memory_nats_publish",
        "memory_nats_readback",
    ]
    assert all(check.status == "passed" for check in report.checks)
    assert _FakePublisher.published
    assert _FakePublisher.published[-1]["memory_realm_id"] == "r_contract"


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
            dependency_unavailable_status="skipped",
        )
    )

    assert report.passed is False
    assert report.summary["skipped_required"] == ["memory_discovery"]
    assert report.checks[0].status == "skipped"


async def test_live_local_contract_nats_unavailable_uses_dependency_policy(
    monkeypatch,
) -> None:
    class UnavailablePublisher:
        def __init__(self, *, event_bus, routes) -> None:
            self.event_bus = event_bus
            self.routes = routes

        async def publish_turn(self, **kwargs) -> None:
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
    monkeypatch.setattr(live_local_contract, "NatsEventBus", _FakeBus)
    monkeypatch.setattr(live_local_contract, "MemoryNatsPublisher", UnavailablePublisher)

    report = await run_live_local_contract(
        LiveLocalContractConfig(
            include_agent_http=False,
            include_agent_admin=False,
            include_admin_gateway=False,
            include_memory=True,
            dependency_unavailable_status="skipped",
        )
    )

    by_name = {check.name: check for check in report.checks}
    assert by_name["memory_nats_publish"].status == "skipped"
    assert report.summary["skipped_required"] == ["memory_nats_publish"]
    assert report.passed is False
