"""Context budgeting and ledger types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextSegmentKind(str, Enum):
    PERSONA = "persona"
    HARNESS_POLICY = "harness_policy"
    RESPONSE_POLICY = "response_policy"
    # Per-turn persona state (mood/energy + per-turn memory/realtime) split out
    # of PERSONA so the identity prefix stays byte-stable for KV-cache reuse.
    PERSONA_STATE = "persona_state"
    REALTIME = "realtime"
    MEMORY = "memory"
    COMMITMENT = "commitment"
    SUMMARY = "summary"
    HISTORY = "history"
    CURRENT_USER = "current_user"


# How each segment behaves across turns, for KV-cache prefix analysis:
#   stable      — invariant within a genome version (belongs in the cached
#                 prefix; anything volatile mixed into it breaks the cache)
#   append_only — grows at the tail, older entries stay byte-identical
#   volatile    — recomputed every turn
#   current     — the user's utterance, always last and always new
_SEGMENT_VOLATILITY: dict[ContextSegmentKind, str] = {
    ContextSegmentKind.PERSONA: "stable",
    ContextSegmentKind.HARNESS_POLICY: "stable",
    ContextSegmentKind.RESPONSE_POLICY: "volatile",
    ContextSegmentKind.PERSONA_STATE: "volatile",
    ContextSegmentKind.HISTORY: "append_only",
    ContextSegmentKind.SUMMARY: "volatile",
    ContextSegmentKind.MEMORY: "volatile",
    ContextSegmentKind.COMMITMENT: "volatile",
    ContextSegmentKind.REALTIME: "volatile",
    ContextSegmentKind.CURRENT_USER: "current",
}


def segment_volatility(kind: ContextSegmentKind) -> str:
    return _SEGMENT_VOLATILITY.get(kind, "volatile")


#: The order segments are assembled in, and the one place the trade-off behind
#: it is decided.
#:
#: A prefix is reusable only up to the first byte that changed, so a volatile
#: segment costs every token *after* it as well as itself. Ordering by
#: volatility therefore is not a preference — it is what decides how much of a
#: turn's prompt has to be re-read. Measured on RK3588 with a `realtime` block
#: whose only change is a two-decimal float: with `append_only` history behind
#: it, 113 tokens were re-read (5.65 s); with history in front of it, 27 tokens
#: (1.69 s). One float, four seconds.
#:
#: `append_only` sits ahead of `volatile` for that reason, and this is the cost
#: of the choice: recent history moves away from the question, and a model
#: attends most to what is nearest it. That effect is not measured here — it
#: needs a listening test, not a token count. If it turns out to matter, the
#: answer is to change this tuple, and only this tuple.
#:
#: `current` is last because it is the question, and the answer must not be
#: predicted from stale context sitting after it.
SEGMENT_ORDER: tuple[str, ...] = ("stable", "append_only", "volatile", "current")


def volatility_rank(kind: ContextSegmentKind) -> int:
    """Where a segment sorts. Derived from its class, never chosen per segment.

    A rank that has to be looked up here is a rank nobody can quietly invert by
    inserting an `append(...)` in a place that reads well — which is how
    `realtime` came to sit in front of `history` and cost four seconds a turn
    without anything recording that it did.
    """

    return SEGMENT_ORDER.index(segment_volatility(kind))


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
    # Set when the mandatory (non-droppable) segments alone exceed the token
    # budget: nothing optional was admitted yet the prompt is already over.
    # Surfaced in turn traces so operators see silent prompt bloat.
    budget_overflow_tokens: int = 0

    @property
    def total_token_estimate(self) -> int:
        return sum(s.token_estimate for s in self.kept_segments)

    def mark_degraded(self, source: str) -> None:
        if source not in self.degraded_sources:
            self.degraded_sources.append(source)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "budget_overflow_tokens": self.budget_overflow_tokens,
            "segments": [
                {
                    "kind": s.kind.value,
                    "source": s.source,
                    "token_estimate": s.token_estimate,
                    "volatility": segment_volatility(s.kind),
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
            ContextSegmentKind.HARNESS_POLICY,
            ContextSegmentKind.CURRENT_USER,
        }
        kept: list[ContextSegment] = []
        dropped: list[DroppedContextSegment] = []

        for seg in segments:
            if seg.kind in mandatory_kinds or not seg.droppable:
                kept.append(seg)

        used = sum(s.token_estimate for s in kept)
        # Mandatory segments are kept even over budget (dropping the persona
        # or the user's own text would be worse), but silently exceeding the
        # budget hides prompt bloat — record the overflow for the trace.
        budget_overflow = max(0, used - self.max_tokens)
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
            budget_overflow_tokens=budget_overflow,
        )


__all__ = [
    "ContextBudget",
    "ContextLedger",
    "ContextSegment",
    "ContextSegmentKind",
    "DroppedContextSegment",
    "segment_volatility",
]
