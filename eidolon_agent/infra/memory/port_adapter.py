"""Combined :class:`MemoryPort` adapter — MCP for reads, NATS for writes."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from eidolon_agent.core.errors import MemoryUnavailableError
from eidolon_agent.core.types.memory import (
    ActiveCommitment,
    ActiveCommitmentReadResult,
    MemoryHit,
    MemoryKind,
    MemoryQueryPlan,
    MemoryRecallResult,
)
from eidolon_agent.core.types.turn_context import build_memory_actor_context
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher

_log = logging.getLogger(__name__)

_MAX_ACTIVE_COMMITMENTS = 10


class EidolonMemoryPort:
    def __init__(
        self,
        *,
        pool: McpClientPool,
        publisher: MemoryNatsPublisher,
    ) -> None:
        self._pool = pool
        self._pub = publisher

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
                    "kg_subjects": list(plan.kg_subjects),
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
                # Do not turn one caller's latency budget into a Realm-wide
                # cancellation. The MCP session is shared by concurrent Agent
                # reads and remains healthy unless the transport reports an
                # actual MemoryUnavailableError.
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
        degraded = bool(raw.get("degraded", False))
        degraded_reason = str(raw.get("degraded_reason") or "") or None
        return MemoryRecallResult(
            context=context,
            hits=hits,
            kg_triples=kg_triples,
            degraded=degraded,
            degraded_reason=degraded_reason if degraded else None,
            diagnostics=_numeric_trace(raw.get("trace")),
        )

    async def read_active_commitments(
        self,
        owner_id: str | None,
        *,
        memory_realm_id: str,
        companion_id: str | None = None,
        device_id: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
        timeout_s: float = 0.2,
    ) -> ActiveCommitmentReadResult:
        """Read a small active-only set from the Realm-bound MCP endpoint."""
        ctx = build_memory_actor_context(
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            device_id=device_id,
            session_id=session_id,
        )
        memory_space_id = ctx.memory_space_id
        bounded_limit = max(1, min(int(limit), _MAX_ACTIVE_COMMITMENTS))
        deadline = asyncio.get_running_loop().time() + timeout_s
        try:
            session = await self._pool.session_for(memory_space_id)
        except MemoryUnavailableError as exc:
            return ActiveCommitmentReadResult(
                degraded=True,
                degraded_reason=_memory_unavailable_reason(exc),
            )

        for attempt in range(2):
            try:
                raw = await asyncio.wait_for(
                    session.call_tool(
                        "eidolon_memory_active_commitments",
                        {
                            "context": ctx.model_dump(mode="json"),
                            "limit": bounded_limit,
                        },
                    ),
                    timeout=_remaining_timeout(deadline),
                )
                response_realm = str(raw.get("memory_space_id") or "")
                if response_realm != memory_space_id:
                    _log.error(
                        "commitment read Realm mismatch expected=%s actual=%s",
                        memory_space_id,
                        response_realm,
                    )
                    return ActiveCommitmentReadResult(
                        degraded=True,
                        degraded_reason="realm_mismatch",
                    )
                active = _records_to_active_commitments(
                    raw.get("commitments") or [],
                    memory_space_id=memory_space_id,
                    limit=bounded_limit,
                )
                total = _non_negative_int(raw.get("total"), default=len(active))
                return ActiveCommitmentReadResult(
                    commitments=active,
                    total=max(total, len(active)),
                    truncated=(raw.get("truncated") is True or total > len(active)),
                )
            except TimeoutError:
                # Active-commitment hydration shares the read session with
                # recall. Its smaller budget must not cancel the recall request.
                return ActiveCommitmentReadResult(
                    degraded=True,
                    degraded_reason="timeout",
                )
            except MemoryUnavailableError as exc:
                reason = _memory_unavailable_reason(exc)
                await self._pool.drop_session(memory_space_id, session=session)
                if attempt == 0 and _remaining_timeout(deadline) > 0:
                    try:
                        session = await self._pool.session_for(memory_space_id)
                    except MemoryUnavailableError as retry_exc:
                        return ActiveCommitmentReadResult(
                            degraded=True,
                            degraded_reason=_memory_unavailable_reason(retry_exc),
                        )
                    continue
                return ActiveCommitmentReadResult(
                    degraded=True,
                    degraded_reason=reason,
                )
            except Exception:
                _log.exception(
                    "active commitment read failed for memory_space=%s",
                    memory_space_id,
                )
                return ActiveCommitmentReadResult(
                    degraded=True,
                    degraded_reason="error",
                )
        return ActiveCommitmentReadResult(
            degraded=True,
            degraded_reason="memory_unavailable",
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
                    valid_from=_parse_memory_datetime(
                        r.get("valid_from") or meta.get("valid_from")
                    ),
                    valid_to=_parse_memory_datetime(r.get("valid_to") or meta.get("valid_to")),
                    metadata=meta,
                )
            )
        except (ValueError, TypeError):
            continue
    return hits


def _records_to_active_commitments(
    records: list[dict],
    *,
    memory_space_id: str,
    limit: int,
) -> list[ActiveCommitment]:
    commitments: list[ActiveCommitment] = []
    for row in records:
        if len(commitments) >= limit:
            break
        if not isinstance(row, dict):
            continue
        try:
            row_memory_space_id = str(row.get("memory_space_id") or "")
            if row_memory_space_id and row_memory_space_id != memory_space_id:
                continue
            status = str(row.get("status") or "")
            predicate = str(row.get("predicate") or "")
            if status not in {"proposed", "confirmed"}:
                continue
            if predicate not in {"promised", "committed_to", "planned_to"}:
                continue
            commitment_id = str(row.get("commitment_id") or "").strip()
            promisor = str(row.get("promisor") or "").strip()
            action = str(row.get("action") or "").strip()
            if not commitment_id or not promisor or not action:
                continue
            commitments.append(
                ActiveCommitment(
                    commitment_id=commitment_id,
                    promisor=promisor,
                    predicate=predicate,
                    action=action,
                    status=status,
                    beneficiaries=_string_tuple(row.get("beneficiaries")),
                    participants=_string_tuple(row.get("participants")),
                    condition=_optional_string(row.get("condition")),
                    due_at=_optional_string(row.get("due_at")),
                    revision=max(1, int(row.get("revision") or 1)),
                    updated_at=str(row.get("updated_at") or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    return commitments


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(cleaned for item in value if (cleaned := str(item).strip()))


def _non_negative_int(value: object, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _optional_string(value: object) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


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
        return dt.replace(tzinfo=UTC)
    return dt


def _numeric_trace(value: object) -> dict[str, float]:
    """Keep timing telemetry while refusing arbitrary backend payloads."""

    if not isinstance(value, dict):
        return {}
    trace: dict[str, float] = {}
    for key, raw in list(value.items())[:64]:
        if (
            not isinstance(key, str)
            or not key.endswith("_ms")
            or len(key) > 64
            or isinstance(raw, bool)
        ):
            continue
        if isinstance(raw, (int, float)):
            trace[key] = float(raw)
    return trace


def _memory_unavailable_reason(exc: MemoryUnavailableError) -> str:
    reason = exc.details.get("reason")
    if isinstance(reason, str) and reason:
        return reason
    return "memory_unavailable"


def _remaining_timeout(deadline: float) -> float:
    return max(0.001, deadline - asyncio.get_running_loop().time())
