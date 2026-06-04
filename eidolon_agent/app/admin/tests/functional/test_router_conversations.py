"""Functional tests for the conversations browse router.

Hits a real SQLite-backed FastAPI app (no mocks). We seed two
conversations belonging to different users, drive a few turns + chat
messages through the same repository write paths the real chat
pipeline uses, then verify the read endpoints return what the admin
UI will render.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from eidolon_agent.app.admin.routers import conversations as conv_router
from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TriageKind, TurnResult, TurnStatus, TurnTrigger
from eidolon_agent.infra.persistence import (
    create_engine,
    create_session_factory,
    ensure_schema,
)
from eidolon_agent.infra.persistence.repositories import (
    SqlChatMessageRepository,
    SqlConversationRepository,
)

pytestmark = pytest.mark.functional


async def _fresh_app(
    tmp_path: Path,
) -> tuple[httpx.AsyncClient, "async_sessionmaker", "AsyncEngine"]:  # type: ignore[name-defined]
    """Build a real SQLite-backed admin app instance.

    The caller is responsible for ``await engine.dispose()`` after
    closing the client — aiosqlite raises an unraisable ResourceWarning
    on __del__ if the connection pool is still alive, which pytest's
    unraisableexception plugin elevates to a test failure.
    """
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(engine)
    factory = create_session_factory(engine)

    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(conv_router.router, prefix="/api/admin")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return client, factory, engine


async def _seed_turn(
    factory,
    *,
    tenant_id: str,
    user_id: str,
    conversation_id: str,
    turn_id: str,
    seq: int,
    user_text: str,
    assistant_text: str,
    started_at: datetime,
) -> None:
    """Write the same shape the real chat path emits."""
    async with factory() as session:
        convo_repo = SqlConversationRepository(session)
        msg_repo = SqlChatMessageRepository(session)
        await convo_repo.ensure_started(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            user_id=user_id,
            agent_instance_id="instance-X",
        )
        await convo_repo.record_turn(
            TurnResult(
                turn_id=turn_id,
                conversation_id=conversation_id,
                status=TurnStatus.OK,
                triage_kind=TriageKind.SIMPLE,
                trigger=TurnTrigger.USER_UTTERANCE,
                seq_count=2,  # we'll append 1 user + 1 assistant message
                started_at=started_at,
                finished_at=started_at,
                latency_first_delta_ms=120,
                total_latency_ms=440,
                tokens_in=20,
                tokens_out=15,
                cost_usd_micro=0,
                model="test/model-1",
                error_code=None,
                seq_in_conversation=seq,
                metadata={
                    "triage_ms": 5,
                    "turn_trace": {
                        "schema_version": "turn_trace.v1",
                        "boundary": "eidolon_agent.brain",
                        "turn": {
                            "turn_id": turn_id,
                            "trigger": "user_utterance",
                            "triage": "simple",
                        },
                        "latency": {
                            "guard_ms": 1,
                            "triage_ms": 2,
                            "compile_ms": 3,
                            "first_delta_ms": 120,
                            "output_ms": 300,
                            "tool_ms": 4,
                            "total_ms": 440,
                        },
                        "context_ledger": {
                            "segments": [
                                {
                                    "kind": "persona",
                                    "source": "personas_service",
                                    "token_estimate": 100,
                                },
                                {
                                    "kind": "memory",
                                    "source": "memory",
                                    "token_estimate": 30,
                                },
                            ],
                            "dropped_segments": [
                                {
                                    "kind": "history",
                                    "source": "history_manager",
                                    "token_estimate": 80,
                                    "reason": "token_budget_exceeded",
                                }
                            ],
                            "degraded_sources": ["memory"],
                            "total_token_estimate": 130,
                        },
                        "memory_trace": {
                            "attempted": True,
                            "degraded": True,
                            "hit_count": 1,
                            "context_injected": True,
                        },
                        "tool_trace": [
                            {
                                "call_id": "tc-1",
                                "name": "get_time",
                                "ok": True,
                                "latency_ms": 4,
                                "cached": False,
                            }
                        ],
                        "privacy": {"mode": "normal"},
                    },
                },
            )
        )
        await msg_repo.append(
            turn_id,
            ChatMessage(
                id=f"{turn_id}-u",
                role=MessageRole.USER,
                content=user_text,
                created_at=started_at,
            ),
        )
        await msg_repo.append(
            turn_id,
            ChatMessage(
                id=f"{turn_id}-a",
                role=MessageRole.ASSISTANT,
                content=assistant_text,
                tokens=15,
                model="test/model-1",
                created_at=started_at,
            ),
        )
        await session.commit()


async def test_list_turns_returns_newest_first_and_filters_by_user(tmp_path) -> None:
    client, factory, engine = await _fresh_app(tmp_path)

    t0 = datetime(2026, 6, 3, 9, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 6, 3, 10, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 3, 11, 0, 0, tzinfo=timezone.utc)

    await _seed_turn(
        factory,
        tenant_id="default", user_id="manson",
        conversation_id="c-manson", turn_id="t-m-1", seq=0,
        user_text="铁锤几岁了？", assistant_text="铁锤今年 10 岁。",
        started_at=t0,
    )
    await _seed_turn(
        factory,
        tenant_id="default", user_id="manson",
        conversation_id="c-manson", turn_id="t-m-2", seq=1,
        user_text="今天天气如何？", assistant_text="多云转晴。",
        started_at=t2,
    )
    await _seed_turn(
        factory,
        tenant_id="default", user_id="alice",
        conversation_id="c-alice", turn_id="t-a-1", seq=0,
        user_text="Hi", assistant_text="Hello!",
        started_at=t1,
    )

    async with client:
        # No filter — all three, newest first
        r = await client.get("/api/admin/conversations/turns")
        assert r.status_code == 200
        body = r.json()
        ids = [t["turn_id"] for t in body["turns"]]
        assert ids == ["t-m-2", "t-a-1", "t-m-1"]

        # Filter by user_id — only manson's two
        r = await client.get("/api/admin/conversations/turns?user_id=manson")
        body = r.json()
        ids = [t["turn_id"] for t in body["turns"]]
        assert ids == ["t-m-2", "t-m-1"]
        # Every row has the cheap-columns shape the schema promises
        for t in body["turns"]:
            assert t["user_id"] == "manson"
            assert t["tenant_id"] == "default"
            assert t["status"] == "ok"
            assert t["model"] == "test/model-1"
            assert t["observability_summary"]["privacy_mode"] == "normal"
            assert t["observability_summary"]["context"]["dropped_count"] == 1
    await engine.dispose()


async def test_get_turn_returns_messages_in_order(tmp_path) -> None:
    client, factory, engine = await _fresh_app(tmp_path)
    t0 = datetime(2026, 6, 3, 9, 0, 0, tzinfo=timezone.utc)
    await _seed_turn(
        factory,
        tenant_id="default", user_id="manson",
        conversation_id="c-1", turn_id="t-1", seq=0,
        user_text="ping", assistant_text="pong",
        started_at=t0,
    )

    async with client:
        r = await client.get("/api/admin/conversations/turns/t-1")

    assert r.status_code == 200
    body = r.json()
    assert body["turn_id"] == "t-1"
    assert body["user_id"] == "manson"
    assert body["latency_first_delta_ms"] == 120
    assert body["turn_trace"]["schema_version"] == "turn_trace.v1"
    assert body["turn_trace"]["boundary"] == "eidolon_agent.brain"
    assert body["metadata"]["turn_trace"]["turn"]["turn_id"] == "t-1"
    summary = body["observability_summary"]
    assert summary["context"]["segment_kinds"] == ["persona", "memory"]
    assert summary["context"]["dropped_kinds"] == ["history"]
    assert summary["memory"]["degraded"] is True
    assert summary["tools"]["names"] == ["get_time"]
    assert summary["latency"]["compile_ms"] == 3
    assert "prompt_fingerprint" in summary
    roles = [m["role"] for m in body["messages"]]
    contents = [m["content"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    assert contents == ["ping", "pong"]
    await engine.dispose()


async def test_get_turn_returns_404_for_unknown_id(tmp_path) -> None:
    client, _factory, engine = await _fresh_app(tmp_path)
    async with client:
        r = await client.get("/api/admin/conversations/turns/does-not-exist")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()
    await engine.dispose()


async def test_list_turns_pagination_cursor(tmp_path) -> None:
    """``before`` filter + ``next_before`` cursor compose into back-paging."""
    client, factory, engine = await _fresh_app(tmp_path)
    base = datetime(2026, 6, 3, 8, 0, 0, tzinfo=timezone.utc)
    # Seed 3 turns at minute intervals
    for i in range(3):
        await _seed_turn(
            factory,
            tenant_id="default", user_id="manson",
            conversation_id=f"c-{i}", turn_id=f"t-{i}", seq=0,
            user_text=f"q{i}", assistant_text=f"a{i}",
            started_at=base.replace(minute=i),
        )

    async with client:
        # First page: limit=2 -> get the 2 newest (t-2, t-1)
        r = await client.get("/api/admin/conversations/turns?limit=2")
        body = r.json()
        ids = [t["turn_id"] for t in body["turns"]]
        assert ids == ["t-2", "t-1"]
        assert body["next_before"] is not None

        # Next page using next_before -> remaining tail (t-0)
        r2 = await client.get(
            f"/api/admin/conversations/turns?limit=2&before={body['next_before']}"
        )
        body2 = r2.json()
        ids2 = [t["turn_id"] for t in body2["turns"]]
        assert ids2 == ["t-0"]
        # Last page (returned < limit) -> cursor exhausted
        assert body2["next_before"] is None
    await engine.dispose()


async def test_list_turns_503_when_session_factory_missing(tmp_path) -> None:
    """Without a wired session_factory the router must not crash with
    AttributeError — it should 503 so the operator can fix the config."""
    app = FastAPI()
    app.state.session_factory = None
    app.include_router(conv_router.router, prefix="/api/admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/admin/conversations/turns")
    assert r.status_code == 503
