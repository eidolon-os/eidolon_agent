"""Functional tests for the long-task admin browse router."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from eidolon_agent.app.admin.routers import long_tasks as long_tasks_router
from eidolon_agent.config.settings import SqliteSettings
from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.infra.persistence import (
    SqlLongTaskRepository,
    create_engine,
    create_session_factory,
    ensure_schema,
)

pytestmark = pytest.mark.functional


async def _fresh_app(
    tmp_path: Path,
) -> tuple[httpx.AsyncClient, async_sessionmaker, AsyncEngine]:
    engine = create_engine(SqliteSettings(path=tmp_path / "agent.sqlite3"))
    await ensure_schema(engine)
    factory = create_session_factory(engine)

    app = FastAPI()
    app.state.session_factory = factory
    app.include_router(long_tasks_router.router, prefix="/api/admin")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return client, factory, engine


async def _seed_task(
    factory,
    *,
    task_id: str,
    user_id: str,
    status: LongTaskStatus = LongTaskStatus.ACCEPTED,
    task_type: str = "research",
) -> None:
    async with factory() as session:
        repo = SqlLongTaskRepository(session)
        await repo.create(
            LongTaskRecord(
                id=task_id,
                provider="mementos",
                status=LongTaskStatus.ACCEPTED,
                tenant_id="tenant-1",
                user_id=user_id,
                agent_instance_id=f"agent-{user_id}",
                conversation_id=f"conv-{task_id}",
                turn_id=f"turn-{task_id}",
                session_id=f"session-{task_id}",
                trace_id=f"trace-{task_id}",
                tool_call_id=f"tool-{task_id}",
                session_key=f"e.{user_id}.20260614",
                task_date="2026-06-14",
                task_key=f"e.{user_id}.20260614.{task_id}",
                task=f"do {task_id}",
                user_text=f"user asked {task_id}",
                task_type=task_type,
                urgency="normal",
                expected_output="structured answer",
                context_summary="relevant context",
                request_payload={"task_id": task_id},
                callback_subject=f"long_task.progress.{task_id}",
            )
        )
        if status is LongTaskStatus.SUCCEEDED:
            await repo.complete(
                task_id,
                result_text="done",
                result_payload={"ok": True},
                artifact_paths=["/tmp/result.txt"],
            )
        elif status is LongTaskStatus.FAILED:
            await repo.fail(
                task_id,
                error_code="worker_error",
                error_message="worker failed",
                error_payload={"retryable": False},
            )
        await session.commit()


async def test_list_long_tasks_filters_without_exposing_raw_db(tmp_path) -> None:
    client, factory, engine = await _fresh_app(tmp_path)
    try:
        await _seed_task(factory, task_id="task-1", user_id="alice")
        await _seed_task(
            factory,
            task_id="task-2",
            user_id="bob",
            status=LongTaskStatus.SUCCEEDED,
            task_type="document_work",
        )

        r = await client.get("/api/admin/long-tasks?user_id=bob&limit=20")
        assert r.status_code == 200
        body = r.json()
        assert body["next_before"] is None
        assert [row["task_id"] for row in body["tasks"]] == ["task-2"]
        assert body["tasks"][0]["status"] == "succeeded"
        assert body["tasks"][0]["result_text"] == "done"
        assert "request_payload" not in body["tasks"][0]
    finally:
        await client.aclose()
        await engine.dispose()


async def test_get_long_task_returns_debug_detail(tmp_path) -> None:
    client, factory, engine = await _fresh_app(tmp_path)
    try:
        await _seed_task(
            factory,
            task_id="task-fail",
            user_id="alice",
            status=LongTaskStatus.FAILED,
        )

        r = await client.get("/api/admin/long-tasks/task-fail")
        assert r.status_code == 200
        body = r.json()
        assert body["task_id"] == "task-fail"
        assert body["request_payload"] == {"task_id": "task-fail"}
        assert body["callback_subject"] == "long_task.progress.task-fail"
        assert body["error_code"] == "worker_error"
        assert body["error_payload"] == {"retryable": False}
    finally:
        await client.aclose()
        await engine.dispose()


async def test_get_long_task_returns_404_for_unknown_id(tmp_path) -> None:
    client, _, engine = await _fresh_app(tmp_path)
    try:
        r = await client.get("/api/admin/long-tasks/missing")
        assert r.status_code == 404
    finally:
        await client.aclose()
        await engine.dispose()
