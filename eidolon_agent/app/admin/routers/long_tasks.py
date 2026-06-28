"""Admin: read-only browse over long-running mementos tasks."""

# ruff: noqa: B008

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from eidolon_agent.core.types.long_task import LongTaskRecord
from eidolon_agent.infra.persistence import EidolonDataLongTaskStore

router = APIRouter()


class LongTaskSummary(BaseModel):
    task_id: str
    provider: str
    status: str
    owner_id: str
    companion_id: str
    memory_realm_id: str | None
    genome_id: str | None
    conversation_id: str | None
    turn_id: str
    trace_id: str | None
    session_key: str
    task_key: str
    task_date: str
    task: str
    task_type: str
    urgency: str
    expected_output: str | None
    progress_summary: str | None
    result_text: str | None
    result_tts_summary: str | None
    error_code: str | None
    error_message: str | None
    worker_id: str | None
    external_status: str | None
    mementos_session_id: str | None
    mementos_conversation_id: str | None
    created_at: datetime | None
    updated_at: datetime | None
    started_at: datetime | None
    submitted_at: datetime | None
    last_progress_at: datetime | None
    last_polled_at: datetime | None
    completed_at: datetime | None


class ListLongTasksResponse(BaseModel):
    tasks: list[LongTaskSummary]
    next_before: datetime | None = None


class LongTaskDetail(LongTaskSummary):
    session_id: str | None
    tool_call_id: str | None
    user_text: str | None
    context_summary: str | None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    request_payload: dict[str, Any] = Field(default_factory=dict)
    mementos_run_id: str | None
    mementos_latest_seq: int | None
    mementos_workspace_dir: str | None
    progress_events: list[dict[str, Any]] = Field(default_factory=list)
    result_payload: dict[str, Any] | None
    artifact_paths: list[str] = Field(default_factory=list)
    error_payload: dict[str, Any] | None
    callback_subject: str | None
    callback_status: str
    callback_attempts: int
    callback_last_error: str | None
    callback_delivered_at: datetime | None
    lease_until: datetime | None
    attempt_count: int
    next_retry_at: datetime | None


def _data_store(request: Request) -> EidolonDataLongTaskStore:
    data_store = getattr(request.app.state, "data_store", None)
    if data_store is None:
        raise HTTPException(503, "data_store not configured on admin app")
    return EidolonDataLongTaskStore(data_store)


@router.get("/long-tasks", response_model=ListLongTasksResponse)
async def list_long_tasks(
    request: Request,
    owner_id: str | None = Query(default=None),
    companion_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    task_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before: datetime | None = Query(
        default=None,
        description="ISO timestamp; only return tasks created strictly before this",
    ),
) -> ListLongTasksResponse:
    store = _data_store(request)
    rows = await store.list_for_admin(
        owner_id=owner_id,
        companion_id=companion_id,
        status=status,
        provider=provider,
        task_type=task_type,
        limit=limit,
        before=before,
    )
    tasks = [_summary(r) for r in rows]
    next_before = tasks[-1].created_at if len(tasks) == limit else None
    return ListLongTasksResponse(tasks=tasks, next_before=next_before)


@router.get("/long-tasks/{task_id}", response_model=LongTaskDetail)
async def get_long_task(request: Request, task_id: str) -> LongTaskDetail:
    store = _data_store(request)
    record = await store.get(task_id)
    if record is None:
        raise HTTPException(404, "long task not found")
    return _detail(record)


def _summary(record: LongTaskRecord) -> LongTaskSummary:
    return LongTaskSummary(
        task_id=record.id,
        provider=record.provider,
        status=record.status.value,
        owner_id=record.owner_id,
        companion_id=record.companion_id,
        memory_realm_id=record.memory_realm_id,
        genome_id=record.genome_id,
        conversation_id=record.conversation_id,
        turn_id=record.turn_id,
        trace_id=record.trace_id,
        session_key=record.session_key,
        task_key=record.task_key,
        task_date=record.task_date,
        task=record.task,
        task_type=record.task_type,
        urgency=record.urgency,
        expected_output=record.expected_output,
        progress_summary=record.progress_summary,
        result_text=record.result_text,
        result_tts_summary=record.result_tts_summary,
        error_code=record.error_code,
        error_message=record.error_message,
        worker_id=record.worker_id,
        external_status=record.external_status,
        mementos_session_id=record.mementos_session_id,
        mementos_conversation_id=record.mementos_conversation_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        started_at=record.started_at,
        submitted_at=record.submitted_at,
        last_progress_at=record.last_progress_at,
        last_polled_at=record.last_polled_at,
        completed_at=record.completed_at,
    )


def _detail(record: LongTaskRecord) -> LongTaskDetail:
    base = _summary(record).model_dump()
    return LongTaskDetail(
        **base,
        session_id=record.session_id,
        tool_call_id=record.tool_call_id,
        user_text=record.user_text,
        context_summary=record.context_summary,
        attachments=record.attachments,
        request_payload=record.request_payload,
        mementos_run_id=record.mementos_run_id,
        mementos_latest_seq=record.mementos_latest_seq,
        mementos_workspace_dir=record.mementos_workspace_dir,
        progress_events=record.progress_events,
        result_payload=record.result_payload,
        artifact_paths=record.artifact_paths,
        error_payload=record.error_payload,
        callback_subject=record.callback_subject,
        callback_status=record.callback_status.value,
        callback_attempts=record.callback_attempts,
        callback_last_error=record.callback_last_error,
        callback_delivered_at=record.callback_delivered_at,
        lease_until=record.lease_until,
        attempt_count=record.attempt_count,
        next_retry_at=record.next_retry_at,
    )
