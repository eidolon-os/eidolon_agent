"""Generic event envelope for the internal EventBus.

Subjects (topic names) are centralized in :mod:`eidolon_agent.infra.events.topics`.
The payload is intentionally a free-form dict — the bus is transport, not a
type system. Strongly-typed wrappers live next to producers/consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    subject: str
    payload: dict[str, Any]
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trace_id: str | None = None
    source: str | None = None  # producing module
    metadata: dict[str, Any] = field(default_factory=dict)
