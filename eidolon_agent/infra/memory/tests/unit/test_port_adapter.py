"""EidolonMemoryPort — read via MCP pool, write via NATS publisher.

The port composes two collaborators (``McpClientPool`` + ``MemoryNatsPublisher``)
that are themselves tested elsewhere. Here we stub them and verify the port's
own behaviour: argument passing, timeout, exception swallowing, hit decoding.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from eidolon_agent.core.errors import MemoryUnavailableError
from eidolon_agent.core.types.memory import MemoryKind, MemoryQueryPlan
from eidolon_agent.infra.memory.port_adapter import EidolonMemoryPort, _records_to_hits

pytestmark = pytest.mark.unit


def _port(*, session_call=None, session_close=None, pub_methods=None):
    session = MagicMock()
    session.call_tool = session_call or AsyncMock(return_value={})
    session.close = session_close or AsyncMock()
    # Capability negotiation: default to "server advertises everything".
    session.supports = AsyncMock(return_value=True)

    pool = MagicMock()
    pool.session_for = AsyncMock(return_value=session)
    pool.drop_session = AsyncMock(return_value=True)
    pool.health = AsyncMock(return_value=True)
    pool.close_all = AsyncMock()

    pub = MagicMock()
    if pub_methods is None:
        pub_methods = {}
    pub.publish_turn = pub_methods.get("publish_turn", AsyncMock())

    return EidolonMemoryPort(pool=pool, publisher=pub), session, pool, pub


# ---- _records_to_hits decoder ---------------------------------------------


def test_records_to_hits_decodes_well_formed_records() -> None:
    raw = [
        {
            "id": "r1",
            "value": "content one",
            "metadata": {"kind": "episode", "similarity": 0.83},
        },
        {
            "key": "r2",
            "value": "content two",
            "metadata": {"kind": "fact", "similarity": 0.5},
        },
    ]
    hits = _records_to_hits(raw)
    assert len(hits) == 2
    assert hits[0].id == "r1" and hits[0].kind is MemoryKind.EPISODE
    assert hits[1].id == "r2" and hits[1].kind is MemoryKind.FACT
    assert hits[0].similarity == pytest.approx(0.83)


def test_records_to_hits_skips_invalid_kind() -> None:
    raw = [
        {"id": "good", "value": "v", "metadata": {"kind": "fragment"}},
        {"id": "bad", "value": "v", "metadata": {"kind": "not_a_kind"}},
    ]
    hits = _records_to_hits(raw)
    assert [h.id for h in hits] == ["good"]


def test_records_to_hits_handles_empty_list() -> None:
    assert _records_to_hits([]) == []


# ---- search ---------------------------------------------------------------


async def test_search_invokes_mcp_search_with_args() -> None:
    call = AsyncMock(
        return_value={
            "records": [
                {"id": "r1", "value": "hello", "metadata": {"kind": "fragment", "similarity": 0.9}}
            ]
        }
    )
    port, _, pool, _ = _port(session_call=call)
    hits = await port.search(
        "owner-1",
        "find x",
        companion_id="companion-1",
        memory_realm_id="realm-1",
        device_id="device-1",
        session_id="s1",
        top_k=3,
    )
    pool.session_for.assert_awaited_once_with("realm-1")
    call.assert_awaited_once()
    name, args = call.await_args.args
    assert name == "eidolon_memory_search"
    assert args["query"] == "find x"
    assert args["top_k"] == 3
    assert args["context"]["owner_id"] == "owner-1"
    assert args["context"]["companion_id"] == "companion-1"
    assert args["context"]["memory_realm_id"] == "realm-1"
    assert args["context"]["device_id"] == "device-1"
    assert args["context"]["memory_space_id"] == "realm-1"
    assert len(hits) == 1
    assert hits[0].content == "hello"


async def test_search_timeout_does_not_close_the_shared_read_session() -> None:
    async def _slow(name, args):
        import asyncio

        await asyncio.sleep(10)
        return {}

    port, _, pool, _ = _port(session_call=_slow)
    assert await port.search("owner-1", "x", memory_realm_id="realm-1", timeout_s=0.01) == []
    pool.drop_session.assert_not_awaited()


async def test_search_drops_session_on_memory_unavailable() -> None:
    call = AsyncMock(side_effect=MemoryUnavailableError("stream closed"))
    port, session, pool, _ = _port(session_call=call)
    assert await port.search("owner-1", "x", memory_realm_id="realm-1") == []
    assert pool.drop_session.await_count == 2
    pool.drop_session.assert_any_await("realm-1", session=session)


async def test_search_retries_once_after_stale_session_unavailable() -> None:
    stale_session = MagicMock()
    stale_session.call_tool = AsyncMock(side_effect=MemoryUnavailableError("stream closed"))
    fresh_session = MagicMock()
    fresh_session.call_tool = AsyncMock(
        return_value={
            "records": [
                {
                    "id": "r1",
                    "value": "用户叫曼森",
                    "metadata": {"kind": "fact", "similarity": 1.0},
                }
            ]
        }
    )
    port, _, pool, _ = _port()
    pool.session_for = AsyncMock(side_effect=[stale_session, fresh_session])

    hits = await port.search("owner-1", "名字", memory_realm_id="realm-1")

    assert [hit.content for hit in hits] == ["用户叫曼森"]
    assert pool.session_for.await_count == 2
    pool.drop_session.assert_awaited_once_with("realm-1", session=stale_session)


async def test_search_returns_empty_on_exception() -> None:
    call = AsyncMock(side_effect=RuntimeError("MCP down"))
    port, _, pool, _ = _port(session_call=call)
    assert await port.search("owner-1", "x", memory_realm_id="realm-1") == []
    pool.drop_session.assert_not_awaited()


# ---- recall_context ------------------------------------------------------


def _plan() -> MemoryQueryPlan:
    return MemoryQueryPlan(
        episodic_query="x", semantic_query="x", episodic_k=3, semantic_k=5, voice=True
    )


async def test_recall_context_returns_context_hits_and_degraded_false() -> None:
    call = AsyncMock(
        return_value={
            "context": "prior conversation summary",
            "records": [
                {
                    "id": "h1",
                    "value": "fact",
                    "memory_time": "2026-05-18T20:00:00Z",
                    "memory_time_source": "occurred_at",
                    "metadata": {"kind": "fact", "similarity": 0.7},
                }
            ],
            "kg_triples": [
                {
                    "id": "kg-1",
                    "subject": "pet:铁锤",
                    "predicate": "holds_role",
                    "object": "边境牧羊犬",
                }
            ],
            "degraded": False,
            "trace": {
                "embedding_ms": 12.5,
                "service_total_ms": 42,
                "unsafe_detail": "must not cross the port",
            },
        }
    )
    port, *_ = _port(session_call=call)
    result = await port.recall_context(
        "owner-1",
        "x",
        companion_id="companion-1",
        memory_realm_id="realm-1",
        device_id="device-1",
        session_id="s1",
        plan=_plan(),
    )
    ctx, hits, degraded = result.context, result.hits, result.degraded
    assert ctx == "prior conversation summary"
    assert [h.id for h in hits] == ["h1"]
    assert hits[0].memory_time is not None
    assert hits[0].memory_time.isoformat() == "2026-05-18T20:00:00+00:00"
    assert hits[0].memory_time_source == "occurred_at"
    assert result.kg_triples == [
        {
            "id": "kg-1",
            "subject": "pet:铁锤",
            "predicate": "holds_role",
            "object": "边境牧羊犬",
        }
    ]
    assert degraded is False
    assert result.degraded_reason is None
    assert result.diagnostics == {
        "embedding_ms": 12.5,
        "service_total_ms": 42.0,
    }
    name, args = call.await_args.args
    assert name == "eidolon_memory_recall_context"
    assert args["top_k"] == 5
    assert args["voice"] is True
    assert args["context"]["owner_id"] == "owner-1"
    assert args["context"]["companion_id"] == "companion-1"
    assert args["context"]["memory_realm_id"] == "realm-1"
    assert args["context"]["device_id"] == "device-1"
    assert args["context"]["session_id"] == "s1"
    assert args["context"]["memory_space_id"] == "realm-1"
    assert args["kg_subjects"] == []
    assert "include_sensitive_kg" not in args


async def test_recall_context_preserves_backend_degraded_state() -> None:
    call = AsyncMock(
        return_value={
            "context": "",
            "records": [],
            "kg_triples": [],
            "degraded": True,
            "degraded_reason": "vector_unavailable",
            "trace": {"service_total_ms": 37.5},
        }
    )
    port, *_ = _port(session_call=call)

    result = await port.recall_context(
        "owner-1",
        "x",
        companion_id="companion-1",
        memory_realm_id="realm-1",
        plan=_plan(),
    )

    assert result.degraded is True
    assert result.degraded_reason == "vector_unavailable"
    assert result.diagnostics == {"service_total_ms": 37.5}


async def test_recall_context_returns_degraded_on_exception() -> None:
    call = AsyncMock(side_effect=RuntimeError("upstream"))
    port, _, pool, _ = _port(session_call=call)
    result = await port.recall_context("owner-1", "x", memory_realm_id="realm-1", plan=_plan())
    ctx, hits, degraded = result.context, result.hits, result.degraded
    assert ctx == ""
    assert hits == []
    assert degraded is True
    assert result.degraded_reason == "error"
    pool.drop_session.assert_not_awaited()


async def test_recall_context_timeout_does_not_close_the_shared_read_session() -> None:
    async def _slow(name, args):
        import asyncio

        await asyncio.sleep(10)
        return {}

    port, _, pool, _ = _port(session_call=_slow)
    result = await port.recall_context(
        "owner-1",
        "x",
        memory_realm_id="realm-1",
        plan=_plan(),
        timeout_s=0.01,
    )

    ctx, hits, degraded = result.context, result.hits, result.degraded
    assert ctx == ""
    assert hits == []
    assert degraded is True
    assert result.degraded_reason == "timeout"
    pool.drop_session.assert_not_awaited()


async def test_recall_context_drops_session_on_memory_unavailable_call() -> None:
    call = AsyncMock(
        side_effect=MemoryUnavailableError(
            "stream closed",
            details={"reason": "memory_stream_closed"},
        )
    )
    port, session, pool, _ = _port(session_call=call)
    result = await port.recall_context("owner-1", "x", memory_realm_id="realm-1", plan=_plan())

    ctx, hits, degraded = result.context, result.hits, result.degraded
    assert ctx == ""
    assert hits == []
    assert degraded is True
    assert result.degraded_reason == "memory_stream_closed"
    assert pool.drop_session.await_count == 2
    pool.drop_session.assert_any_await("realm-1", session=session)


async def test_recall_context_retries_once_after_stale_session_unavailable() -> None:
    stale_session = MagicMock()
    stale_session.call_tool = AsyncMock(
        side_effect=MemoryUnavailableError(
            "stream closed",
            details={"reason": "memory_stream_closed"},
        )
    )
    fresh_session = MagicMock()
    fresh_session.call_tool = AsyncMock(
        return_value={
            "context": "用户叫曼森，在北京化工大学读书。",
            "records": [
                {
                    "id": "h1",
                    "value": "用户叫曼森，在北京化工大学读书。",
                    "metadata": {"kind": "fact", "similarity": 1.0},
                }
            ],
        }
    )
    port, _, pool, _ = _port()
    pool.session_for = AsyncMock(side_effect=[stale_session, fresh_session])

    result = await port.recall_context(
        "owner-1",
        "我叫什么？我在哪里读书？",
        memory_realm_id="realm-1",
        plan=_plan(),
        timeout_s=1.0,
    )

    ctx, hits, degraded = result.context, result.hits, result.degraded
    assert degraded is False
    assert ctx == "用户叫曼森，在北京化工大学读书。"
    assert [hit.id for hit in hits] == ["h1"]
    assert pool.session_for.await_count == 2
    pool.drop_session.assert_awaited_once_with("realm-1", session=stale_session)


async def test_recall_context_returns_route_reason_on_unavailable_session() -> None:
    port, _, pool, _ = _port()
    pool.session_for = AsyncMock(
        side_effect=MemoryUnavailableError(
            "no route",
            details={"memory_space_id": "realm-1", "reason": "no_memory_route"},
        )
    )

    result = await port.recall_context("owner-1", "x", memory_realm_id="realm-1", plan=_plan())

    ctx, hits, degraded = result.context, result.hits, result.degraded
    assert ctx == ""
    assert hits == []
    assert degraded is True
    assert result.degraded_reason == "no_memory_route"


# ---- active commitments --------------------------------------------------


async def test_active_commitments_are_realm_bound_bounded_and_active_only() -> None:
    records = [
        {
            "commitment_id": f"commitment-{idx}",
            "memory_space_id": "realm-1",
            "promisor": "小忆",
            "predicate": "promised",
            "action": f"陪 owner 散步 {idx}",
            "status": "confirmed",
            "beneficiaries": ["owner"],
            "participants": ["铁锤"],
            "condition": "天气合适",
            "due_at": "2026-07-20T10:00:00+08:00",
            "revision": idx + 1,
            "updated_at": "2026-07-17T10:00:00+08:00",
        }
        for idx in range(12)
    ]
    records.insert(
        1,
        {
            **records[0],
            "commitment_id": "terminal",
            "status": "fulfilled",
        },
    )
    records.insert(
        2,
        {
            **records[0],
            "commitment_id": "other-realm",
            "memory_space_id": "realm-2",
        },
    )
    call = AsyncMock(
        return_value={
            "memory_space_id": "realm-1",
            "total": 12,
            "truncated": True,
            "commitments": records,
        }
    )
    port, _, pool, _ = _port(session_call=call)

    result = await port.read_active_commitments(
        "owner-1",
        companion_id="companion-1",
        memory_realm_id="realm-1",
        device_id="device-1",
        session_id="s1",
        limit=99,
    )

    pool.session_for.assert_awaited_once_with("realm-1")
    name, args = call.await_args.args
    assert name == "eidolon_memory_active_commitments"
    assert args["limit"] == 10
    assert args["context"]["memory_realm_id"] == "realm-1"
    assert args["context"]["owner_id"] == "owner-1"
    assert args["context"]["companion_id"] == "companion-1"
    assert result.degraded is False
    assert len(result.commitments) == 10
    assert result.total == 12
    assert result.truncated is True
    assert all(item.status == "confirmed" for item in result.commitments)
    assert all(item.commitment_id not in {"terminal", "other-realm"} for item in result.commitments)
    assert result.commitments[0].participants == ("铁锤",)


async def test_active_commitments_fail_closed_on_response_realm_mismatch() -> None:
    call = AsyncMock(return_value={"memory_space_id": "realm-2", "commitments": []})
    port, *_ = _port(session_call=call)

    result = await port.read_active_commitments(
        "owner-1",
        memory_realm_id="realm-1",
    )

    assert result.commitments == []
    assert result.degraded is True
    assert result.degraded_reason == "realm_mismatch"


# ---- write_turn / forget --------------------------------------------------


async def test_write_turn_delegates_to_publisher() -> None:
    port, _, _, pub = _port()
    await port.write_turn(
        "owner-1",
        "companion-1",
        "realm-1",
        "device-1",
        "s1",
        "turn-1",
        "hi",
        "hello",
        metadata={"k": "v"},
    )
    pub.publish_turn.assert_awaited_once_with(
        owner_id="owner-1",
        companion_id="companion-1",
        memory_realm_id="realm-1",
        device_id="device-1",
        session_id="s1",
        turn_id="turn-1",
        owner_text="hi",
        assistant_text="hello",
        metadata={"k": "v"},
    )


# ---- health / close -------------------------------------------------------


async def test_health_delegates_to_pool() -> None:
    port, _, pool, _ = _port()
    assert await port.health() is True
    pool.health.assert_awaited_once()


async def test_close_calls_pool_close_all() -> None:
    port, _, pool, _ = _port()
    await port.close()
    pool.close_all.assert_awaited_once()
