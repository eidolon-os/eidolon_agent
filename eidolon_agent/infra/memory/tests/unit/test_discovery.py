from __future__ import annotations

from types import SimpleNamespace

import pytest

from eidolon_agent.config.settings import MemoryEndpoint, NatsSettings
from eidolon_agent.infra.memory.discovery import DiscoveryResponse, MemoryRoutingTable
from eidolon_agent.infra.memory.mcp_client import _decode_call_tool_result
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher
from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort

pytestmark = pytest.mark.unit

@pytest.mark.asyncio
async def test_discovery_replaces_routes_and_filters_unreachable(monkeypatch):
    monkeypatch.setenv("EIDOLON_MEMORY_MCP_TOKEN", "secret")
    discovery = DiscoveryResponse.model_validate(
        {
            "version": 1,
            "nats": {
                "url": "nats://memory:4222",
                "stream": "MEMORY_TURNS",
                "turn_subject_template": "mem.turn.{user_id}",
                "cmd_subject_template": "mem.cmd.{user_id}",
            },
            "users": [
                {
                    "user_id": "alice",
                    "enabled": True,
                    "mcp_http_url": "http://127.0.0.1:8031/mcp",
                    "mcp_auth": {
                        "type": "bearer",
                        "token_env": "EIDOLON_MEMORY_MCP_TOKEN",
                    },
                    "agent_reachable": True,
                },
                {
                    "user_id": "bob",
                    "enabled": False,
                    "mcp_http_url": "http://127.0.0.1:8032/mcp",
                    "agent_reachable": True,
                },
                {
                    "user_id": "charlie",
                    "enabled": True,
                    "mcp_http_url": "http://127.0.0.1:8033/mcp",
                    "agent_reachable": False,
                },
            ],
        }
    )
    routes = MemoryRoutingTable.from_static(endpoints=[], nats=NatsSettings())

    await routes.replace_from_discovery(discovery)

    alice = await routes.route_for("alice")
    assert alice is not None
    assert alice.mcp_url == "http://127.0.0.1:8031/mcp"
    assert alice.bearer_token == "secret"
    assert await routes.route_for("bob") is None
    assert await routes.route_for("charlie") is None
    assert await routes.endpoint_count() == 1
    assert await routes.render_turn_subject("alice") == "mem.turn.alice"
    assert await routes.render_cmd_subject("alice") == "mem.cmd.alice"


@pytest.mark.asyncio
async def test_static_routes_remain_fallback():
    routes = MemoryRoutingTable.from_static(
        endpoints=[
            MemoryEndpoint(
                user_id="default",
                mcp_url="http://127.0.0.1:8030/mcp",
                bearer_token="local-token",
            )
        ],
        nats=NatsSettings(url="nats://static:4222"),
    )

    route = await routes.route_for("default")
    assert route is not None
    assert route.bearer_token == "local-token"
    assert await routes.nats_url() == "nats://static:4222"
    assert await routes.render_turn_subject("default") == "agent.memory.conversation.turn.default"


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
                "turn_subject_template": "turns.{user_id}",
                "cmd_subject_template": "cmds.{user_id}",
            },
            "users": [],
        }
    )
    routes = MemoryRoutingTable.from_static(endpoints=[], nats=NatsSettings())
    await routes.replace_from_discovery(discovery)
    bus = CaptureBus()
    pub = MemoryNatsPublisher(event_bus=bus, routes=routes)

    await pub.publish_turn(
        user_id="alice",
        session_id="s1",
        turn_id="t1",
        user_text="hi",
        assistant_text="hello",
    )
    await pub.publish_kg_add(
        user_id="alice",
        subject="self",
        predicate="likes",
        object_="oolong",
    )

    turn_event, turn_persistent = bus.events[0]
    cmd_event, cmd_persistent = bus.events[1]
    assert turn_persistent is True
    assert turn_event.subject == "turns.alice"
    assert turn_event.payload["turn_id"] == "t1"
    assert cmd_persistent is True
    assert cmd_event.subject == "cmds.alice"
    assert cmd_event.payload["kind"] == "kg_add_triple"
    assert cmd_event.payload["issuer"] == "agent"
    assert cmd_event.payload["request_id"]
    assert "command" not in cmd_event.payload


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
        async def session_for(self, user_id):
            assert user_id == "alice"
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

    context, hits, degraded = await port.recall_context(
        "alice",
        "铁锤是什么",
        plan=MemoryQueryPlan(semantic_k=3),
        timeout_s=1.0,
    )

    assert degraded is False
    assert context == "铁锤是一只狗。"
    assert hits and hits[0].content == "铁锤是一只狗。"
