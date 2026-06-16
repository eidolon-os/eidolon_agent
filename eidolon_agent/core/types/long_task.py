"""Long-running coworker task records."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


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
    tenant_id: str
    user_id: str
    conversation_id: str | None
    turn_id: str
    session_id: str | None
    trace_id: str | None
    session_key: str
    task_date: str
    task_key: str
    task: str
    user_text: str = ""
    agent_instance_id: str | None = None
    tool_call_id: str | None = None
    task_type: str = "other"
    urgency: str = "normal"
    expected_output: str = ""
    context_summary: str = ""
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


_SAFE_USER_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def safe_user_key(user_id: str) -> str:
    """Return a compact key-safe user id segment."""

    if _SAFE_USER_RE.fullmatch(user_id) and not user_id.startswith("b64_"):
        return user_id
    encoded = base64.urlsafe_b64encode(user_id.encode("utf-8")).decode("ascii")
    return f"b64_{encoded.rstrip('=')}"


def user_id_from_safe_key(segment: str) -> str:
    """Reverse :func:`safe_user_key`."""

    if not segment.startswith("b64_"):
        return segment
    encoded = segment.removeprefix("b64_")
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8")


def session_key_for(user_id: str, local_date: date | str) -> str:
    """Daily Mementos session key: ``e.{safe_user}.{yyyymmdd}``."""

    if isinstance(local_date, date):
        yyyymmdd = local_date.strftime("%Y%m%d")
    else:
        yyyymmdd = local_date.replace("-", "")
    return f"e.{safe_user_key(user_id)}.{yyyymmdd}"


def parse_session_key(session_key: str) -> tuple[str, str]:
    """Return ``(user_id, yyyymmdd)`` from ``e.{safe_user}.{yyyymmdd}``."""

    prefix, segment, yyyymmdd = session_key.split(".", 2)
    if prefix != "e" or not segment or len(yyyymmdd) != 8:
        raise ValueError(f"invalid long-task session key: {session_key}")
    return user_id_from_safe_key(segment), yyyymmdd


def task_key_for(session_key: str, task_id: str) -> str:
    return f"{session_key}.{task_id[:12]}"


__all__ = [
    "CallbackStatus",
    "LongTaskRecord",
    "LongTaskStatus",
    "parse_session_key",
    "safe_user_key",
    "session_key_for",
    "task_key_for",
    "user_id_from_safe_key",
]
