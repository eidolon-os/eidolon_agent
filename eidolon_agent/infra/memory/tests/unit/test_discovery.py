from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from eidolon_agent.config.settings import MemoryEndpoint, NatsSettings
from eidolon_agent.infra.memory.discovery import (
    DiscoveryResponse,
    MemoryDiscoveryClient,
    MemoryRoutingTable,
)
from eidolon_agent.infra.memory.mcp_client import _decode_call_tool_result
from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_discovery_client_ignores_shell_proxy_env(monkeypatch):
    captured = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            return httpx.Response(
                200,
                json={
                    "version": 2,
                    "nats": {"url": "nats://127.0.0.1:4222"},
                    "memory_realms": [],
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr("eidolon_agent.infra.memory.discovery.httpx.AsyncClient", FakeAsyncClient)

    discovery = await MemoryDiscoveryClient(
        discovery_url="http://127.0.0.1:8020/api/discovery/agent-routing",
    ).fetch()

    assert captured["trust_env"] is False
    assert discovery.nats.url == "nats://127.0.0.1:4222"


@pytest.mark.asyncio
async def test_discovery_replaces_routes_and_filters_unreachable(monkeypatch):
    monkeypatch.setenv("EIDOLON_MEMORY_MCP_TOKEN", "secret")
    discovery = DiscoveryResponse.model_validate(
        {
            "version": 2,
            "nats": {
                "url": "nats://memory:4222",
                "stream": "MEMORY_TURNS",
                "turn_subject_template": "mem.turn.{memory_space_token}",
                "cmd_subject_template": "mem.cmd.{memory_space_token}",
            },
            "memory_realms": [
                {
                    "memory_space_id": "r_benchmark_default",
                    "memory_realm_id": "r_benchmark_default",
                    "owner_id": "benchmark",
                    "enabled": True,
                    "mcp_http_url": "http://127.0.0.1:8031/mcp",
                    "ops_mcp_http_url": "http://127.0.0.1:8031/ops/mcp",
                    "mcp_auth": {
                        "type": "bearer",
                        "token_env": "EIDOLON_MEMORY_MCP_TOKEN",
                    },
                    "agent_reachable": True,
                },
                {
                    "memory_space_id": "r_benchmark_disabled",
                    "memory_realm_id": "r_benchmark_disabled",
                    "owner_id": "benchmark",
                    "enabled": False,
                    "mcp_http_url": "http://127.0.0.1:8032/mcp",
                    "ops_mcp_http_url": "http://127.0.0.1:8032/ops/mcp",
                    "agent_reachable": True,
                },
                {
                    "memory_space_id": "r_benchmark_unreachable",
                    "memory_realm_id": "r_benchmark_unreachable",
                    "owner_id": "benchmark",
                    "enabled": True,
                    "mcp_http_url": "http://127.0.0.1:8033/mcp",
                    "ops_mcp_http_url": "http://127.0.0.1:8033/ops/mcp",
                    "agent_reachable": False,
                },
            ],
        }
    )
    routes = MemoryRoutingTable.from_static(endpoints=[], nats=NatsSettings())

    await routes.replace_from_discovery(discovery)

    alice = await routes.route_for("r_benchmark_default")
    assert alice is not None
    assert alice.mcp_url == "http://127.0.0.1:8031/mcp"
    assert alice.ops_mcp_url == "http://127.0.0.1:8031/ops/mcp"
    assert alice.bearer_token == "secret"
    assert await routes.route_for("r_benchmark_disabled") is None
    assert await routes.route_for("r_benchmark_unreachable") is None
    bob_route, bob_reason = await routes.route_status_for("r_benchmark_disabled")
    charlie_route, charlie_reason = await routes.route_status_for("r_benchmark_unreachable")
    ghost_route, ghost_reason = await routes.route_status_for("r_benchmark_ghost")
    assert bob_route is None and bob_reason == "memory_route_disabled"
    assert charlie_route is None and charlie_reason == "memory_route_unreachable"
    assert ghost_route is None and ghost_reason == "no_memory_route"
    assert await routes.endpoint_count() == 1
    assert await routes.memory_space_ids() == ["r_benchmark_default"]
    assert await routes.render_turn_subject("r_benchmark_default") == (
        "mem.turn.b64_cl9iZW5jaG1hcmtfZGVmYXVsdA"
    )
    assert await routes.render_cmd_subject("r_benchmark_default") == (
        "mem.cmd.b64_cl9iZW5jaG1hcmtfZGVmYXVsdA"
    )


@pytest.mark.asyncio
async def test_static_routes_remain_fallback():
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                memory_space_id="default.alice.default",
                mcp_url="http://127.0.0.1:8030/mcp",
                bearer_token="local-token",
            )
        ],
        nats=NatsSettings(url="nats://static:4222"),
    )

    route = await routes.route_for("default.alice.default")
    assert route is not None
    assert route.bearer_token == "local-token"
    assert route.ops_mcp_url == "http://127.0.0.1:8030/mcp"
    assert await routes.nats_url() == "nats://static:4222"
    assert await routes.render_turn_subject("default.alice.default") == (
        "eidolon.memory.turn.b64_ZGVmYXVsdC5hbGljZS5kZWZhdWx0"
    )


def test_mcp_decode_prefers_structured_content_and_unwraps_fastmcp_result():
    result = SimpleNamespace(
        isError=False,
        structuredContent={"result": [{"key": "k1", "value": "v1"}]},
        content=[],
    )

    assert _decode_call_tool_result(result) == [{"key": "k1", "value": "v1"}]


def test_mcp_decode_text_json():
    block = SimpleNamespace(type="text", text='{"result": {"context": "ok"}}')
    result = SimpleNamespace(isError=False, structuredContent=None, content=[block])

    assert _decode_call_tool_result(result) == {"context": "ok"}


@pytest.mark.asyncio
async def test_recall_context_calls_mcp_directly():
    class Session:
        async def call_tool(self, name, arguments):
            assert name == "eidolon_memory_recall_context"
            assert arguments["context"]["owner_id"] == "benchmark"
            assert arguments["context"]["companion_id"] == "test"
            assert arguments["context"]["memory_realm_id"] == "r_benchmark_default"
            assert arguments["context"]["device_id"] == "admin-console"
            assert arguments["context"]["memory_space_id"] == "r_benchmark_default"
            return {
                "context": "铁锤是一只狗。",
                "records": [
                    {
                        "key": "pet_铁锤",
                        "value": "铁锤是一只狗。",
                        "metadata": {"similarity": 0.9},
                    }
                ],
            }

    class Pool:
        async def session_for(self, memory_space_id):
            assert memory_space_id == "r_benchmark_default"
            return Session()

        async def close_all(self):
            pass

        async def health(self):
            return True

    from eidolon_agent.core.types.memory import MemoryQueryPlan

    port = EidolonMemoryPort(pool=Pool())

    result = await port.recall_context(
        "benchmark",
        "铁锤是什么",
        plan=MemoryQueryPlan(semantic_k=3),
        timeout_s=1.0,
        companion_id="test",
        memory_realm_id="r_benchmark_default",
        device_id="admin-console",
    )

    assert result.degraded is False
    assert result.context == "铁锤是一只狗。"
    assert result.hits and result.hits[0].content == "铁锤是一只狗。"
