"""Long-running coworker task records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from eidolon_sdk.biz.long_tasks import (
    owner_id_from_safe_key,
    parse_session_key,
    safe_owner_key,
    session_key_for,
    task_key_for,
)


class LongTaskStatus(str, Enum):
    ACCEPTED = "accepted"
    CREATED = "created"
    QUEUED = "queued"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class CallbackStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class LongTaskRecord:
    id: str
    provider: str
    status: LongTaskStatus
    owner_id: str
    companion_id: str
    conversation_id: str | None
    turn_id: str
    session_id: str | None
    trace_id: str | None
    session_key: str
    task_date: str
    task_key: str
    task: str
    user_text: str = ""
    memory_realm_id: str | None = None
    genome_id: str | None = None
    # Source device (from caller identity / turns.source_device_id). Carried so a
    # proactive report can be routed back to the device (Phase 3 wake).
    device_id: str | None = None
    tool_call_id: str | None = None
    task_type: str = "other"
    urgency: str = "normal"
    expected_output: str = ""
    context_summary: str = ""
    # Delegation contract (Anthropic multi-agent lesson: a subagent needs a
    # bounded objective, an output contract, a tool/step budget, and a rough
    # duration to work well). 0 tool_budget = coworker default; empty
    # expected_duration_hint = unspecified.
    tool_budget: int = 0
    expected_duration_hint: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    request_payload: dict[str, Any] = field(default_factory=dict)
    mementos_session_id: str | None = None
    mementos_conversation_id: str | None = None
    mementos_run_id: str | None = None
    mementos_latest_seq: int | None = None
    mementos_workspace_dir: str | None = None
    progress_summary: str | None = None
    progress_events: list[dict[str, Any]] = field(default_factory=list)
    result_text: str | None = None
    result_tts_summary: str | None = None
    result_payload: dict[str, Any] | None = None
    artifact_paths: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    error_payload: dict[str, Any] | None = None
    callback_subject: str | None = None
    callback_status: CallbackStatus = CallbackStatus.PENDING
    callback_attempts: int = 0
    callback_last_error: str | None = None
    callback_delivered_at: datetime | None = None
    worker_id: str | None = None
    lease_until: datetime | None = None
    attempt_count: int = 0
    next_retry_at: datetime | None = None
    external_status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    submitted_at: datetime | None = None
    last_progress_at: datetime | None = None
    last_polled_at: datetime | None = None
    completed_at: datetime | None = None


__all__ = [
    "CallbackStatus",
    "LongTaskRecord",
    "LongTaskStatus",
    "owner_id_from_safe_key",
    "parse_session_key",
    "safe_owner_key",
    "session_key_for",
    "task_key_for",
]
