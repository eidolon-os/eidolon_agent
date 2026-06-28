"""Combined :class:`MemoryPort` adapter — MCP for reads, NATS for writes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from eidolon_agent.core.errors import MemoryUnavailableError
from eidolon_agent.core.types.identity import build_memory_actor_context
from eidolon_agent.core.types.memory import (
    MemoryHit,
    MemoryKind,
    MemoryQueryPlan,
    MemoryRecallResult,
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
        owner_id: str,
        query: str,
        *,
        top_k: int = 5,
        scope: MemoryScope = MemoryScope.ALL,
        voice: bool = True,
        timeout_s: float = 0.2,
        companion_id: str,
        memory_realm_id: str,
        device_id: str,
        session_id: str = "default",
    ) -> list[MemoryHit]:
        ctx = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        memory_space_id = ctx.memory_space_id
        session = await self._pool.session_for(memory_space_id)
        try:
            raw = await asyncio.wait_for(
                session.call_tool(
                    "eidolon_memory_search",
                    {
                        "query": query,
                        "context": ctx.model_dump(mode="json"),
                        "top_k": top_k,
                    },
                ),
                timeout=timeout_s,
            )
        except TimeoutError:
            _log.warning("memory search timed out for memory_space=%s", memory_space_id)
            await self._pool.drop_session(memory_space_id, session=session)
            return []
        except MemoryUnavailableError:
            _log.warning("memory search unavailable for memory_space=%s", memory_space_id)
            await self._pool.drop_session(memory_space_id, session=session)
            return []
        except Exception:
            _log.exception("memory search failed for memory_space=%s", memory_space_id)
            return []
        return _records_to_hits(raw.get("records") or [])

    async def recall_context(
        self,
        owner_id: str,
        query: str,
        *,
        plan: MemoryQueryPlan,
        timeout_s: float = 0.2,
        companion_id: str,
        memory_realm_id: str,
        device_id: str,
        session_id: str = "default",
    ) -> MemoryRecallResult:
        ctx = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        memory_space_id = ctx.memory_space_id
        try:
            session = await self._pool.session_for(memory_space_id)
        except MemoryUnavailableError as exc:
            reason = _memory_unavailable_reason(exc)
            _log.warning(
                "memory recall unavailable for memory_space=%s reason=%s",
                memory_space_id,
                reason,
            )
            return MemoryRecallResult(degraded=True, degraded_reason=reason)
        try:
            raw = await asyncio.wait_for(
                session.call_tool(
                    "eidolon_memory_recall_context",
                    {
                        "query": query,
                        "context": ctx.model_dump(mode="json"),
                        "top_k": plan.semantic_k,
                        "voice": plan.voice,
                        "include_kg": True,
                        "include_sensitive_kg": False,
                    },
                ),
                timeout=timeout_s,
            )
        except TimeoutError:
            _log.warning("memory recall timed out for memory_space=%s", memory_space_id)
            await self._pool.drop_session(memory_space_id, session=session)
            return MemoryRecallResult(degraded=True, degraded_reason="timeout")
        except MemoryUnavailableError as exc:
            reason = _memory_unavailable_reason(exc)
            _log.warning(
                "memory recall unavailable for memory_space=%s reason=%s",
                memory_space_id,
                reason,
            )
            await self._pool.drop_session(memory_space_id, session=session)
            return MemoryRecallResult(degraded=True, degraded_reason=reason)
        except Exception:
            _log.exception("memory recall failed for memory_space=%s", memory_space_id)
            return MemoryRecallResult(degraded=True, degraded_reason="error")
        context = raw.get("context", "") or ""
        hits = _records_to_hits(raw.get("records") or [])
        kg_triples = raw.get("kg_triples") or []
        if not isinstance(kg_triples, list):
            kg_triples = []
        return MemoryRecallResult(
            context=context,
            hits=hits,
            kg_triples=kg_triples,
            degraded=False,
        )

    async def write_turn(
        self,
        owner_id: str,
        companion_id: str,
        memory_realm_id: str,
        device_id: str,
        session_id: str,
        turn_id: str,
        owner_text: str,
        assistant_text: str,
        *,
        metadata: dict | None = None,
    ) -> None:
        await self._pub.publish_turn(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
            turn_id=turn_id,
            owner_text=owner_text,
            assistant_text=assistant_text,
            metadata=metadata,
        )

    async def assert_fact(
        self,
        owner_id: str,
        companion_id: str,
        memory_realm_id: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        confidence: float = 0.9,
    ) -> None:
        await self._pub.publish_kg_add(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            subject=subject,
            predicate=predicate,
            object_=object_,
            confidence=confidence,
        )

    async def forget(
        self,
        owner_id: str,
        companion_id: str,
        memory_realm_id: str,
        device_id: str,
        query: str,
        *,
        session_id: str = "default",
    ) -> int:
        # The memory service exposes ``eidolon_memory_forget`` via MCP in newer versions;
        # if absent, we no-op safely. Production should branch on capability negotiation.
        ctx = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        session = await self._pool.session_for(ctx.memory_space_id)
        try:
            result = await session.call_tool(
                "eidolon_memory_forget",
                {"query": query, "context": ctx.model_dump(mode="json")},
            )
            return int(result.get("removed", 0))
        except Exception:
            _log.warning(
                "memory forget unsupported or failed for memory_space=%s",
                ctx.memory_space_id,
            )
            return 0

    async def health(self) -> bool:
        return await self._pool.health()

    async def close(self) -> None:
        await self._pool.close_all()


def _records_to_hits(records: list[dict]) -> list[MemoryHit]:
    hits: list[MemoryHit] = []
    for r in records:
        try:
            meta = r.get("metadata") or {}
            hits.append(
                MemoryHit(
                    id=str(r.get("id") or r.get("key") or ""),
                    content=str(r.get("value", "")),
                    kind=MemoryKind(meta.get("kind", "fragment")),
                    similarity=float(meta.get("similarity", 0.0)),
                    memory_time=_parse_memory_datetime(
                        r.get("memory_time") or meta.get("memory_time")
                    ),
                    memory_time_source=(
                        str(r.get("memory_time_source") or meta.get("memory_time_source") or "")
                        or None
                    ),
                    valid_from=_parse_memory_datetime(r.get("valid_from") or meta.get("valid_from")),
                    valid_to=_parse_memory_datetime(r.get("valid_to") or meta.get("valid_to")),
                    metadata=meta,
                )
            )
        except (ValueError, TypeError):
            continue
    return hits


def _parse_memory_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _memory_unavailable_reason(exc: MemoryUnavailableError) -> str:
    reason = exc.details.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return "memory_unavailable"
