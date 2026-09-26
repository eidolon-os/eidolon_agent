"""Interpretation records — what every interpreter said about one input.

A record keeps the request and each adapter's raw answer (even one the
orchestrator rejected), so a new implementation can be replayed offline
against real traffic and compared with what production decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from eidolon_sdk.biz.interpretation import InterpretationRequest, InterpretationResult

AdapterRole = Literal["primary", "shadow"]

# ``AdapterRun.error`` values that are not InterpretationError codes.
RUN_EXCEPTION = "EXCEPTION"
RUN_CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class AdapterRun:
    """One adapter's answer to one request.

    ``result`` is exactly what the adapter returned. When ``rejected`` is set
    (the ``validate_proposal`` failure code), the orchestrator treated that
    result as abstained. ``error`` is an InterpretationError code, or
    ``EXCEPTION`` / ``CANCELLED`` when the adapter produced nothing at all.
    """

    adapter: str
    role: AdapterRole
    latency_ms: float
    result: InterpretationResult | None = None
    error: str | None = None
    detail: str | None = None
    rejected: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "role": self.role,
            "latency_ms": round(self.latency_ms, 3),
            "result": None if self.result is None else self.result.model_dump(mode="json"),
            "error": self.error,
            "detail": self.detail,
            "rejected": self.rejected,
        }


@dataclass(frozen=True, slots=True)
class InterpretationRecord:
    """The request, the primary's answer, and every shadow's answer."""

    recorded_at: str  # ISO-8601 UTC, when the last answer arrived
    request: InterpretationRequest
    primary: AdapterRun
    shadows: tuple[AdapterRun, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "recorded_at": self.recorded_at,
            "request": self.request.model_dump(mode="json"),
            "primary": self.primary.to_json(),
            "shadows": [run.to_json() for run in self.shadows],
        }
