from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from eidolon_sdk.memory import unwrap_memory_payload

from eidolon_agent.config.settings import MemoryEndpoint, NatsSettings
from eidolon_agent.infra.memory.discovery import (
    DiscoveryResponse,
    MemoryDiscoveryClient,
    MemoryRoutingTable,
)
from eidolon_agent.infra.memory.mcp_client import _decode_call_tool_result
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher
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
                    "version": 1,
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
            "version": 1,
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
                    "companion_id": "mochi",
                    "enabled": True,
                    "mcp_http_url": "http://127.0.0.1:8031/mcp",
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
                    "companion_id": "disabled",
                    "enabled": False,
                    "mcp_http_url": "http://127.0.0.1:8032/mcp",
                    "agent_reachable": True,
                },
                {
                    "memory_space_id": "r_benchmark_unreachable",
                    "memory_realm_id": "r_benchmark_unreachable",
                    "owner_id": "benchmark",
                    "companion_id": "unreachable",
                    "enabled": True,
                    "mcp_http_url": "http://127.0.0.1:8033/mcp",
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
    assert await routes.nats_url() == "nats://static:4222"
    assert await routes.render_turn_subject("default.alice.default") == (
        "eidolon.memory.turn.b64_ZGVmYXVsdC5hbGljZS5kZWZhdWx0"
    )


@pytest.mark.asyncio
async def test_nats_publisher_uses_discovered_subjects_and_memory_schema():
    class CaptureBus:
        def __init__(self):
            self.events = []

        async def publish(self, event, *, persistent=False):
            self.events.append((event, persistent))

    discovery = DiscoveryResponse.model_validate(
        {
            "nats": {
                "url": "nats://memory:4222",
                "stream": "MEMORY_TURNS",
                "turn_subject_template": "turns.{memory_space_id}",
                "cmd_subject_template": "cmds.{memory_space_id}",
            },
            "memory_realms": [],
        }
    )
    routes = MemoryRoutingTable.from_static(endpoints=[], nats=NatsSettings())
    await routes.replace_from_discovery(discovery)
    bus = CaptureBus()
    pub = MemoryNatsPublisher(event_bus=bus, routes=routes)

    await pub.publish_turn(
        owner_id="benchmark",
        companion_id="test",
        memory_realm_id="r_benchmark_default",
        device_id="admin-console",
        session_id="s1",
        turn_id="t1",
        owner_text="hi",
        assistant_text="hello",
    )
    await pub.publish_structured_intent(
        owner_id="benchmark",
        companion_id="test",
        memory_realm_id="r_benchmark_default",
        subject="self",
        predicate="likes",
        object_="oolong",
        source_event_id="t1",
        tool_call_id="call-structured",
    )
    await pub.publish_verbatim_intent(
        owner_id="benchmark",
        companion_id="test",
        memory_realm_id="r_benchmark_default",
        device_id="admin-console",
        session_id="s1",
        text="用户 最终验证时间 2026-06-28 20:00",
        source_event_id="t1",
        tool_call_id="call-verbatim",
        confidence=0.95,
        tags=["kg_fallback"],
    )
    await pub.publish_commitment_intent(
        owner_id="benchmark",
        companion_id="test",
        memory_realm_id="r_benchmark_default",
        promisor="self",
        predicate="promised",
        action="带 companion:test 去恐龙园",
        raw_claim="以后带你去恐龙园",
        source_event_id="t1",
        tool_call_id="call-commitment",
        operation="update",
        target_id="commitment:abc",
        participants=["friend:小明"],
        status="fulfilled",
    )

    turn_event, turn_persistent = bus.events[0]
    cmd_event, cmd_persistent = bus.events[1]
    confirmed_event, confirmed_persistent = bus.events[2]
    commitment_event, commitment_persistent = bus.events[3]
    assert turn_persistent is True
    assert turn_event.subject == "turns.b64_cl9iZW5jaG1hcmtfZGVmYXVsdA"
    turn_payload = unwrap_memory_payload(turn_event.payload)
    assert turn_payload["turn_id"] == "t1"
    assert turn_payload["context"]["owner_id"] == "benchmark"
    assert turn_payload["context"]["companion_id"] == "test"
    assert turn_payload["context"]["memory_realm_id"] == "r_benchmark_default"
    assert turn_payload["context"]["device_id"] == "admin-console"
    assert cmd_persistent is True
    assert cmd_event.subject == "cmds.b64_cl9iZW5jaG1hcmtfZGVmYXVsdA"
    cmd_payload = unwrap_memory_payload(cmd_event.payload)
    assert cmd_payload["kind"] == "memory_intent"
    assert cmd_payload["issuer"] == "agent"
    assert cmd_payload["request_id"]
    assert "command" not in cmd_payload
    assert cmd_payload["intent"]["source_event_id"] == "t1"
    assert cmd_payload["intent"]["tool_call_id"] == "call-structured"
    assert cmd_payload["intent"]["subject"] == "self"
    assert cmd_payload["intent"]["predicate"] == "likes"
    assert cmd_payload["intent"]["object"] == "oolong"
    assert confirmed_persistent is True
    assert confirmed_event.subject == "cmds.b64_cl9iZW5jaG1hcmtfZGVmYXVsdA"
    confirmed_payload = unwrap_memory_payload(confirmed_event.payload)
    assert confirmed_payload["kind"] == "memory_intent"
    assert confirmed_payload["issuer"] == "agent"
    intent = confirmed_payload["intent"]
    assert intent["raw_claim"] == "用户 最终验证时间 2026-06-28 20:00"
    assert intent["source_event_id"] == "t1"
    assert intent["tool_call_id"] == "call-verbatim"
    assert intent["attributes"]["source_device_id"] == "admin-console"
    assert intent["attributes"]["source_instance_id"] == "test"
    assert intent["attributes"]["session_id"] == "s1"
    assert intent["attributes"]["tags"] == ["kg_fallback"]
    assert commitment_persistent is True
    assert commitment_event.subject == "cmds.b64_cl9iZW5jaG1hcmtfZGVmYXVsdA"
    commitment_payload = unwrap_memory_payload(commitment_event.payload)
    commitment = commitment_payload["intent"]
    assert commitment["intent_type"] == "commitment"
    assert commitment["operation_hint"] == "update"
    assert commitment["target_id"] == "commitment:abc"
    assert commitment["subject"] == "self"
    assert commitment["predicate"] == "promised"
    assert commitment["object"] == "带 companion:test 去恐龙园"
    assert commitment["attributes"] == {
        "participants": ["friend:小明"],
        "status": "fulfilled",
    }


@pytest.mark.asyncio
async def test_commitment_publisher_requires_target_for_update():
    pub = MemoryNatsPublisher(event_bus=object())

    with pytest.raises(ValueError, match="requires target_id"):
        await pub.publish_commitment_intent(
            owner_id="benchmark",
            companion_id="test",
            memory_realm_id="r_benchmark_default",
            promisor="self",
            predicate="promised",
            action="带 companion:test 去恐龙园",
            raw_claim="补充一个朋友",
            source_event_id="t1",
            tool_call_id="call-commitment",
            operation="update",
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

    port = EidolonMemoryPort(
        pool=Pool(),
        publisher=MemoryNatsPublisher(event_bus=object()),
    )

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
