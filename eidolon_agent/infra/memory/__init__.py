"""Adapter for the external eidolon-memory service.

Reads go through MCP Streamable HTTP (per-memory-space agent_runner port).
Writes go through NATS JetStream on ``eidolon.memory.turn.<memory_space_token>``.
Explicit KG mutations go through ``eidolon.memory.cmd.<memory_space_token>``.
"""

from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort
from eidolon_agent.infra.memory.strategy import MemoryStrategy

__all__ = ["EidolonMemoryPort", "MemoryStrategy"]
