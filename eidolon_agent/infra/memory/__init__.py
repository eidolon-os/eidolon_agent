"""Adapter for the external eidolon-memory service.

Reads go through MCP Streamable HTTP (per-memory-space agent_runner port).
Writes go through NATS JetStream on ``eidolon.memory.turn.<memory_space_token>``.
Explicit KG mutations go through ``eidolon.memory.cmd.<memory_space_token>``.
"""

from eidolon_agent.infra.memory.discovery import (
    MemoryRoutingTable,
    build_initial_memory_routes,
)
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher
from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort

__all__ = [
    "EidolonMemoryPort",
    "McpClientPool",
    "MemoryNatsPublisher",
    "MemoryRoutingTable",
    "build_initial_memory_routes",
]
