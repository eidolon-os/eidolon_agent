"""Functional tests for the long-task admin browse router."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from eidolon_data import DataSettings, DataStore
from fastapi import FastAPI

from eidolon_agent.app.admin.routers import long_tasks as long_tasks_router
from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.infra.persistence import EidolonDataLongTaskStore

pytestmark = pytest.mark.functional


async def _fresh_app(
    tmp_path: Path,
) -> tuple[httpx.AsyncClient, DataStore]:
    store = DataStore.open(DataSettings(sqlite_path=str(tmp_path / "eidolon.sqlite3")))
    await store.init_schema()
    app = FastAPI()
    app.state.data_store = store
    app.include_router(long_tasks_router.router, prefix="/api/admin")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return client, store


async def _seed_task(
    store: DataStore,
    *,
    task_id: str,
    owner_id: str,
    status: LongTaskStatus = LongTaskStatus.ACCEPTED,
    task_type: str = "research",
) -> None:
    await store.owner_service.create_owner(owner_id=owner_id, display_name=owner_id)
    await store.workspace_provisioning.provision_workspace(
        owner_id=owner_id,
        companion_id=f"agent-{owner_id}",
        genome_id=f"genome-{owner_id}",
        realm_id=f"realm-{owner_id}",
    )
    task_store = EidolonDataLongTaskStore(store)
    await task_store.create(
        LongTaskRecord(
            id=task_id,
            provider="mementos",
            status=LongTaskStatus.ACCEPTED,
            owner_id=owner_id,
            companion_id=f"agent-{owner_id}",
            conversation_id=f"conv-{task_id}",
            turn_id=f"turn-{task_id}",
            session_id=f"session-{task_id}",
            trace_id=f"trace-{task_id}",
            tool_call_id=f"tool-{task_id}",
            session_key=f"e.{owner_id}.20260614",
            task_date="2026-06-14",
            task_key=f"e.{owner_id}.20260614.{task_id}",
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
        await task_store.complete(
            task_id,
            result_text="done",
            result_payload={"ok": True},
            artifact_paths=["/tmp/result.txt"],
        )
    elif status is LongTaskStatus.FAILED:
        await task_store.mark_failed(
            task_id,
            error_code="worker_error",
            error_message="worker failed",
            error_payload={"retryable": False},
        )


async def test_list_long_tasks_filters_without_exposing_raw_db(tmp_path) -> None:
    client, store = await _fresh_app(tmp_path)
    try:
        await _seed_task(store, task_id="task-1", owner_id="alice")
        await _seed_task(
            store,
            task_id="task-2",
            owner_id="bob",
            status=LongTaskStatus.SUCCEEDED,
            task_type="document_work",
        )

        r = await client.get("/api/admin/long-tasks?owner_id=bob&limit=20")
        assert r.status_code == 200
        body = r.json()
        assert body["next_before"] is None
        assert [row["task_id"] for row in body["tasks"]] == ["task-2"]
        assert body["tasks"][0]["status"] == "succeeded"
        assert body["tasks"][0]["result_text"] == "done"
        assert "request_payload" not in body["tasks"][0]
    finally:
        await client.aclose()
        await store.close()


async def test_get_long_task_returns_debug_detail(tmp_path) -> None:
    client, store = await _fresh_app(tmp_path)
    try:
        await _seed_task(
            store,
            task_id="task-fail",
            owner_id="alice",
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
        await store.close()


async def test_get_long_task_returns_404_for_unknown_id(tmp_path) -> None:
    client, store = await _fresh_app(tmp_path)
    try:
        r = await client.get("/api/admin/long-tasks/missing")
        assert r.status_code == 404
    finally:
        await client.aclose()
        await store.close()


async def test_list_long_tasks_503_when_data_store_missing() -> None:
    app = FastAPI()
    app.include_router(long_tasks_router.router, prefix="/api/admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/admin/long-tasks")
    assert r.status_code == 503
