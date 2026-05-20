"""Dispatch — handing complex work to the external workstation agent.

The companion stays responsive (returns ACK + HANDOFF immediately) while the
workstation chews on the long task and streams Progress back via NATS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ProgressKind(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ExternalTask:
    """Self-contained task description sent to the workstation agent."""

    id: str  # uuid7
    tenant_id: str
    user_id: str
    natural_language: str  # the verbatim user ask
    structured: dict = field(default_factory=dict)  # optional pre-parsed slots
    priority: int = 5  # 1=highest .. 9=lowest
    submitted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DispatchHandle:
    task_id: str
    progress_subject: str  # NATS subject to subscribe for updates


@dataclass(frozen=True, slots=True)
class Progress:
    task_id: str
    kind: ProgressKind
    note: str = ""
    payload: dict = field(default_factory=dict)
    ts: datetime | None = None
