"""Adapter for the external eidolon-memory service.

Reads go through MCP Streamable HTTP (per-user agent_runner port).
Writes go through NATS JetStream on ``agent.memory.conversation.turn.<user_id>``.
Explicit KG mutations go through ``agent.memory.cmd.<user_id>``.
"""

from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort
from eidolon_agent.infra.memory.strategy import MemoryStrategy

__all__ = ["EidolonMemoryPort", "MemoryStrategy"]
