"""Context budgeting and ledger types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextSegmentKind(str, Enum):
    PERSONA = "persona"
    REALTIME = "realtime"
    MEMORY = "memory"
    SUMMARY = "summary"
    HISTORY = "history"
    CURRENT_USER = "current_user"


@dataclass(frozen=True, slots=True)
class ContextSegment:
    kind: ContextSegmentKind
    content: str
    source: str
    token_estimate: int
    priority: int = 100
    droppable: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DroppedContextSegment:
    kind: ContextSegmentKind
    source: str
    token_estimate: int
    reason: str


@dataclass(slots=True)
class ContextLedger:
    kept_segments: list[ContextSegment] = field(default_factory=list)
    dropped_segments: list[DroppedContextSegment] = field(default_factory=list)
    degraded_sources: list[str] = field(default_factory=list)

    @property
    def total_token_estimate(self) -> int:
        return sum(s.token_estimate for s in self.kept_segments)

    def mark_degraded(self, source: str) -> None:
        if source not in self.degraded_sources:
            self.degraded_sources.append(source)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "segments": [
                {
                    "kind": s.kind.value,
                    "source": s.source,
                    "token_estimate": s.token_estimate,
                    "metadata": s.metadata,
                }
                for s in self.kept_segments
            ],
            "dropped_segments": [
                {
                    "kind": s.kind.value,
                    "source": s.source,
                    "token_estimate": s.token_estimate,
                    "reason": s.reason,
                }
                for s in self.dropped_segments
            ],
            "degraded_sources": list(self.degraded_sources),
            "total_token_estimate": self.total_token_estimate,
        }


class ContextBudget:
    def __init__(self, *, max_tokens: int) -> None:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        self.max_tokens = max_tokens

    def prune(self, segments: list[ContextSegment]) -> tuple[list[ContextSegment], ContextLedger]:
        mandatory_kinds = {
            ContextSegmentKind.PERSONA,
            ContextSegmentKind.CURRENT_USER,
        }
        kept: list[ContextSegment] = []
        dropped: list[DroppedContextSegment] = []

        for seg in segments:
            if seg.kind in mandatory_kinds or not seg.droppable:
                kept.append(seg)

        used = sum(s.token_estimate for s in kept)
        kept_ids = {id(s) for s in kept}
        optional = [
            (idx, seg)
            for idx, seg in enumerate(segments)
            if id(seg) not in kept_ids
        ]
        optional.sort(key=lambda item: (-item[1].priority, item[0]))

        for _idx, seg in optional:
            if used + seg.token_estimate <= self.max_tokens:
                kept.append(seg)
                kept_ids.add(id(seg))
                used += seg.token_estimate
            else:
                dropped.append(
                    DroppedContextSegment(
                        kind=seg.kind,
                        source=seg.source,
                        token_estimate=seg.token_estimate,
                        reason="token_budget_exceeded",
                    )
                )

        ordered_kept = [s for s in segments if id(s) in kept_ids]
        return ordered_kept, ContextLedger(
            kept_segments=ordered_kept,
            dropped_segments=dropped,
        )


__all__ = [
    "ContextBudget",
    "ContextLedger",
    "ContextSegment",
    "ContextSegmentKind",
    "DroppedContextSegment",
]
