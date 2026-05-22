"""Combined :class:`MemoryPort` adapter — MCP for reads, NATS for writes."""

from __future__ import annotations

import asyncio
import logging

from eidolon_agent.core.types.memory import (
    MemoryHit,
    MemoryKind,
    MemoryQueryPlan,
    MemoryScope,
)
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher

_log = logging.getLogger(__name__)


class EidolonMemoryPort:
    def __init__(
        self,
        *,
        pool: McpClientPool,
        publisher: MemoryNatsPublisher,
    ) -> None:
        self._pool = pool
        self._pub = publisher

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        top_k: int = 5,
        scope: MemoryScope = MemoryScope.ALL,
        voice: bool = True,
        timeout_s: float = 0.2,
    ) -> list[MemoryHit]:
        session = await self._pool.session_for(user_id)
        try:
            raw = await asyncio.wait_for(
                session.call_tool(
                    "eidolon_memory_search",
                    {"query": query, "top_k": top_k},
                ),
                timeout=timeout_s,
            )
        except Exception:
            _log.exception("memory search failed for user=%s", user_id)
            return []
        return _records_to_hits(raw.get("records") or [])

    async def recall_context(
        self,
        user_id: str,
        query: str,
        *,
        plan: MemoryQueryPlan,
        timeout_s: float = 0.2,
    ) -> tuple[str, list[MemoryHit], bool]:
        session = await self._pool.session_for(user_id)
        try:
            raw = await asyncio.wait_for(
                session.call_tool(
                    "eidolon_memory_recall_context",
                    {
                        "query": query,
                        "top_k": plan.semantic_k,
                        "voice": plan.voice,
                        "include_kg": True,
                        "include_sensitive_kg": False,
                    },
                ),
                timeout=timeout_s,
            )
        except Exception:
            _log.exception("memory recall failed for user=%s", user_id)
            return "", [], True
        context = raw.get("context", "") or ""
        hits = _records_to_hits(raw.get("records") or [])
        return context, hits, False

    async def write_turn(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
        *,
        metadata: dict | None = None,
    ) -> None:
        await self._pub.publish_turn(
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
            user_text=user_text,
            assistant_text=assistant_text,
            metadata=metadata,
        )

    async def assert_fact(
        self,
        user_id: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        confidence: float = 0.9,
    ) -> None:
        await self._pub.publish_kg_add(
            user_id=user_id,
            subject=subject,
            predicate=predicate,
            object_=object_,
            confidence=confidence,
        )

    async def forget(self, user_id: str, query: str) -> int:
        # The memory service exposes ``eidolon_memory_forget`` via MCP in newer versions;
        # if absent, we no-op safely. Production should branch on capability negotiation.
        session = await self._pool.session_for(user_id)
        try:
            result = await session.call_tool("eidolon_memory_forget", {"query": query})
            return int(result.get("removed", 0))
        except Exception:
            _log.warning("memory forget unsupported or failed for user=%s", user_id)
            return 0

    async def health(self) -> bool:
        return await self._pool.health()

    async def close(self) -> None:
        await self._pool.close_all()


def _records_to_hits(records: list[dict]) -> list[MemoryHit]:
    hits: list[MemoryHit] = []
    for r in records:
        try:
            hits.append(
                MemoryHit(
                    id=str(r.get("id") or r.get("key") or ""),
                    content=str(r.get("value", "")),
                    kind=MemoryKind(r.get("metadata", {}).get("kind", "fragment")),
                    similarity=float(r.get("metadata", {}).get("similarity", 0.0)),
                    metadata=r.get("metadata") or {},
                )
            )
        except (ValueError, TypeError):
            continue
    return hits
