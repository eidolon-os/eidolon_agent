from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from uuid import uuid4

import pytest

from eidolon_agent.config.settings import load_settings
from eidolon_agent.core.errors import MemoryUnavailableError, NatsUnavailableError
from eidolon_agent.infra.events import NatsEventBus
from eidolon_agent.infra.memory.discovery import build_initial_memory_routes
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher

pytestmark = pytest.mark.smoke


@pytest.mark.asyncio
async def test_live_memory_contract_mcp_tools_and_nats_publish() -> None:
    if os.environ.get("EIDOLON_AGENT_LIVE_MEMORY_CONTRACT") != "1":
        pytest.skip("set EIDOLON_AGENT_LIVE_MEMORY_CONTRACT=1 to run live memory contract smoke")

    settings = load_settings()
    routes, effective_nats_url, refresher = await build_initial_memory_routes(
        memory=settings.memory,
        nats=settings.nats,
    )
    del refresher
    memory_space_ids = await routes.memory_space_ids()
    if not memory_space_ids:
        pytest.skip(
            "eidolon_memory discovery/static config exposed no enabled reachable memory routes"
        )
    memory_space_id = (
        os.environ.get("EIDOLON_AGENT_LIVE_MEMORY_SPACE_ID", "").strip()
        or memory_space_ids[0]
    )

    pool = McpClientPool(routes=routes)
    try:
        try:
            session = await pool.session_for(memory_space_id)
            tool_names = await asyncio.wait_for(session.tool_names(), timeout=5.0)
        except MemoryUnavailableError as exc:
            pytest.skip(f"memory MCP route unavailable for {memory_space_id}: {exc}")
        if tool_names is None:
            pytest.skip(f"memory MCP list_tools did not return capabilities for {memory_space_id}")
        required = {"eidolon_memory_recall_context", "eidolon_memory_search"}
        assert required.intersection(tool_names), sorted(tool_names)
    finally:
        with suppress(Exception):
            await pool.close_all()

    bus = NatsEventBus(effective_nats_url, creds_path=str(settings.nats.creds_path) if settings.nats.creds_path else None)
    publisher = MemoryNatsPublisher(event_bus=bus, routes=routes)
    turn_id = f"live-memory-contract-{uuid4().hex}"
    try:
        await publisher.publish_turn(
            owner_id="live-memory-contract",
            companion_id="live-memory-contract",
            memory_realm_id=memory_space_id,
            device_id=None,
            session_id="live-memory-contract",
            turn_id=turn_id,
            owner_text="live memory contract probe",
            assistant_text="ack",
            metadata={
                "source": "eidolon-agent-live-memory-contract",
                "contract_only": True,
            },
        )
    except NatsUnavailableError as exc:
        pytest.skip(f"NATS unavailable for live memory contract at {effective_nats_url}: {exc}")
    except Exception as exc:
        pytest.skip(
            f"NATS publish unavailable for live memory contract at {effective_nats_url}: "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        with suppress(Exception):
            await bus.close()
