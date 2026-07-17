"""Combined :class:`MemoryPort` adapter — MCP for reads, NATS for writes."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from eidolon_agent.core.errors import MemoryUnavailableError
from eidolon_agent.core.types.identity import build_memory_actor_context
from eidolon_agent.core.types.memory import (
    MemoryForgetCandidate,
    MemoryForgetOutcome,
    MemoryForgetPreview,
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
        owner_id: str | None,
        query: str,
        *,
        memory_realm_id: str,
        top_k: int = 5,
        scope: MemoryScope = MemoryScope.ALL,
        voice: bool = True,
        timeout_s: float = 0.2,
        companion_id: str | None = None,
        device_id: str | None = None,
        session_id: str | None = None,
    ) -> list[MemoryHit]:
        ctx = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        memory_space_id = ctx.memory_space_id
        deadline = asyncio.get_running_loop().time() + timeout_s
        for attempt in range(2):
            try:
                session = await self._pool.session_for(memory_space_id)
            except MemoryUnavailableError:
                _log.warning("memory search unavailable for memory_space=%s", memory_space_id)
                return []
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
                    timeout=_remaining_timeout(deadline),
                )
                return _records_to_hits(raw.get("records") or [])
            except TimeoutError:
                _log.warning("memory search timed out for memory_space=%s", memory_space_id)
                await self._pool.drop_session(memory_space_id, session=session)
                return []
            except MemoryUnavailableError:
                _log.warning(
                    "memory search unavailable for memory_space=%s attempt=%d",
                    memory_space_id,
                    attempt + 1,
                )
                await self._pool.drop_session(memory_space_id, session=session)
                if attempt == 0 and _remaining_timeout(deadline) > 0:
                    continue
                return []
            except Exception:
                _log.exception("memory search failed for memory_space=%s", memory_space_id)
                return []
        return []

    async def _recall_context_once(
        self,
        session,
        *,
        query: str,
        ctx,
        plan: MemoryQueryPlan,
        deadline: float,
    ) -> dict:
        return await asyncio.wait_for(
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
            timeout=_remaining_timeout(deadline),
        )

    async def recall_context(
        self,
        owner_id: str | None,
        query: str,
        *,
        memory_realm_id: str,
        plan: MemoryQueryPlan,
        timeout_s: float = 0.2,
        companion_id: str | None = None,
        device_id: str | None = None,
        session_id: str | None = None,
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
        deadline = asyncio.get_running_loop().time() + timeout_s
        for attempt in range(2):
            try:
                raw = await self._recall_context_once(
                    session,
                    query=query,
                    ctx=ctx,
                    plan=plan,
                    deadline=deadline,
                )
                break
            except TimeoutError:
                _log.warning("memory recall timed out for memory_space=%s", memory_space_id)
                await self._pool.drop_session(memory_space_id, session=session)
                return MemoryRecallResult(degraded=True, degraded_reason="timeout")
            except MemoryUnavailableError as exc:
                reason = _memory_unavailable_reason(exc)
                _log.warning(
                    "memory recall unavailable for memory_space=%s reason=%s attempt=%d",
                    memory_space_id,
                    reason,
                    attempt + 1,
                )
                await self._pool.drop_session(memory_space_id, session=session)
                if attempt == 0 and _remaining_timeout(deadline) > 0:
                    try:
                        session = await self._pool.session_for(memory_space_id)
                    except MemoryUnavailableError as retry_exc:
                        reason = _memory_unavailable_reason(retry_exc)
                        return MemoryRecallResult(
                            degraded=True,
                            degraded_reason=reason,
                        )
                    continue
                return MemoryRecallResult(degraded=True, degraded_reason=reason)
            except Exception:
                _log.exception("memory recall failed for memory_space=%s", memory_space_id)
                return MemoryRecallResult(degraded=True, degraded_reason="error")
        else:
            return MemoryRecallResult(degraded=True, degraded_reason="memory_unavailable")
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
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
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
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        subject: str,
        predicate: str,
        object_: str,
        *,
        source_event_id: str,
        tool_call_id: str,
        confidence: float = 0.9,
    ) -> str:
        return await self._pub.publish_structured_intent(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            subject=subject,
            predicate=predicate,
            object_=object_,
            source_event_id=source_event_id,
            tool_call_id=tool_call_id,
            confidence=confidence,
        )

    async def write_confirmed_fact(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        session_id: str | None,
        text: str,
        *,
        source_event_id: str,
        tool_call_id: str,
        confidence: float = 0.99,
        tags: list[str] | None = None,
    ) -> str:
        return await self._pub.publish_verbatim_intent(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
            text=text,
            source_event_id=source_event_id,
            tool_call_id=tool_call_id,
            confidence=confidence,
            tags=tags,
        )

    async def apply_commitment(
        self,
        owner_id,
        companion_id,
        memory_realm_id,
        promisor,
        predicate,
        action,
        raw_claim,
        *,
        source_event_id,
        tool_call_id,
        operation="confirm",
        target_id=None,
        beneficiaries=None,
        participants=None,
        condition=None,
        due_at=None,
        status=None,
        confidence=0.99,
    ) -> str:
        return await self._pub.publish_commitment_intent(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            promisor=promisor,
            predicate=predicate,
            action=action,
            raw_claim=raw_claim,
            source_event_id=source_event_id,
            tool_call_id=tool_call_id,
            operation=operation,
            target_id=target_id,
            beneficiaries=beneficiaries,
            participants=participants,
            condition=condition,
            due_at=due_at,
            status=status,
            confidence=confidence,
        )

    async def preview_forget(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        query: str,
        *,
        action: str = "archive",
        session_id: str | None = None,
    ) -> MemoryForgetPreview:
        resolved_action = "delete" if action == "delete" else "archive"
        ctx = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        try:
            session = await self._pool.session_for(ctx.memory_space_id)
            if not await session.supports("eidolon_memory_forget_preview"):
                _log.info(
                    "memory forget preview absent for memory_space=%s",
                    ctx.memory_space_id,
                )
                return MemoryForgetPreview(
                    status="unavailable",
                    target=query,
                    action=resolved_action,
                    error="forget preview capability unavailable",
                )
            result = await session.call_tool(
                "eidolon_memory_forget_preview",
                {"target": query, "action": resolved_action},
            )
            status = str(result.get("status") or "failed")
            if status not in {"preview", "not_found", "too_broad"}:
                status = "failed"
            candidates = [
                MemoryForgetCandidate(
                    id=str(
                        item.get("drawer_id") or item.get("id") or item.get("key") or ""
                    ),
                    content=str(
                        item.get("text") or item.get("content") or item.get("value") or ""
                    ),
                    score=float(item.get("score") or 0.0),
                )
                for item in (result.get("candidates") or [])
                if isinstance(item, dict)
            ]
            return MemoryForgetPreview(
                status=status,
                target=str(result.get("target") or query),
                action=resolved_action,
                candidates=candidates,
                requires_explicit_confirmation=bool(
                    result.get("requires_explicit_confirmation")
                ),
                confirmation_token=str(result.get("confirmation_token") or ""),
                expires_at=str(result.get("expires_at") or ""),
                error=str(result.get("error") or ""),
            )
        except Exception as exc:
            _log.warning(
                "memory forget preview failed for memory_space=%s",
                ctx.memory_space_id,
            )
            return MemoryForgetPreview(
                status="failed",
                target=query,
                action=resolved_action,
                error=str(exc),
            )

    async def confirm_forget(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        confirmation_token: str,
        *,
        session_id: str | None = None,
        wait_applied_seconds: float = 2.0,
    ) -> MemoryForgetOutcome:
        ctx = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        try:
            session = await self._pool.session_for(ctx.memory_space_id)
            if not await session.supports("eidolon_memory_forget_confirm"):
                return MemoryForgetOutcome(
                    status="unavailable",
                    action="archive",
                    error="forget confirm capability unavailable",
                )
            result = await session.call_tool(
                "eidolon_memory_forget_confirm",
                {
                    "confirmation_token": confirmation_token,
                    "wait_applied_seconds": wait_applied_seconds,
                },
            )
        except Exception as exc:
            _log.warning(
                "memory forget confirm failed for memory_space=%s",
                ctx.memory_space_id,
            )
            return MemoryForgetOutcome(
                status="failed",
                action="archive",
                error=str(exc),
            )
        status = str(result.get("status") or "failed")
        if status not in {"accepted", "applied", "failed"}:
            status = "failed"
        action = "delete" if result.get("action") == "delete" else "archive"
        return MemoryForgetOutcome(
            status=status,
            action=action,
            request_id=str(result.get("request_id") or ""),
            drawer_ids=[str(item) for item in (result.get("drawer_ids") or [])],
            error=str(result.get("error") or ""),
        )

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


def _remaining_timeout(deadline: float) -> float:
    return max(0.001, deadline - asyncio.get_running_loop().time())
