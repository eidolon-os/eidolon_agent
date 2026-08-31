"""Adapter for the external eidolon-memory service.

Reads go through MCP Streamable HTTP (per-memory-space agent_runner port).
Completed turns go through the domain ``HistoryFanout`` durable path.
"""

from eidolon_agent.infra.memory.discovery import (
    MemoryRoutingTable,
    build_initial_memory_routes,
)
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort

__all__ = [
    "EidolonMemoryPort",
    "McpClientPool",
    "MemoryRoutingTable",
    "build_initial_memory_routes",
]
