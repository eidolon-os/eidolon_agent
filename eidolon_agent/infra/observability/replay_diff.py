"""Replay diff gate for rollout safety.

The gate compares prompt-safe turn traces, not raw prompts. That keeps CI
artifacts free of private message text while still catching changes in context
shape, tool decisions, and latency.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ReplayDiffThresholds:
    max_prompt_change_ratio: float = 0.05
    max_tool_decision_change_ratio: float = 0.0
    max_latency_regression_ratio: float = 0.25
    max_latency_regression_ms: int = 150

    def to_metadata(self) -> dict[str, Any]:
        return {
            "max_prompt_change_ratio": self.max_prompt_change_ratio,
            "max_tool_decision_change_ratio": self.max_tool_decision_change_ratio,
            "max_latency_regression_ratio": self.max_latency_regression_ratio,
            "max_latency_regression_ms": self.max_latency_regression_ms,
        }


@dataclass(frozen=True, slots=True)
class ReplayTurnSnapshot:
    key: str
    prompt_fingerprint: str
    tool_decision: tuple[str, ...] = ()
    first_delta_ms: int | None = None
    total_ms: int | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "prompt_fingerprint": self.prompt_fingerprint,
            "tool_decision": list(self.tool_decision),
            "first_delta_ms": self.first_delta_ms,
            "total_ms": self.total_ms,
        }


@dataclass(frozen=True, slots=True)
class ReplayLatencyRegression:
    index: int
    key: str
    field: str
    baseline_ms: int
    candidate_ms: int
    delta_ms: int
    delta_ratio: float

    def to_metadata(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "key": self.key,
            "field": self.field,
            "baseline_ms": self.baseline_ms,
            "candidate_ms": self.candidate_ms,
            "delta_ms": self.delta_ms,
            "delta_ratio": round(self.delta_ratio, 4),
        }


@dataclass(frozen=True, slots=True)
class ReplayGateReport:
    passed: bool
    total_compared: int
    thresholds: ReplayDiffThresholds
    prompt_changed: list[dict[str, Any]] = field(default_factory=list)
    tool_decision_changed: list[dict[str, Any]] = field(default_factory=list)
    latency_regressions: list[ReplayLatencyRegression] = field(default_factory=list)
    missing_turns: int = 0
    extra_turns: int = 0

    def to_metadata(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total_compared": self.total_compared,
            "thresholds": self.thresholds.to_metadata(),
            "summary": {
                "prompt_changed": len(self.prompt_changed),
                "tool_decision_changed": len(self.tool_decision_changed),
                "latency_regressions": len(self.latency_regressions),
                "missing_turns": self.missing_turns,
                "extra_turns": self.extra_turns,
            },
            "prompt_changed": self.prompt_changed,
            "tool_decision_changed": self.tool_decision_changed,
            "latency_regressions": [r.to_metadata() for r in self.latency_regressions],
        }


def snapshot_from_turn_trace(trace: dict[str, Any], *, fallback_key: str) -> ReplayTurnSnapshot:
    turn = trace.get("turn") or {}
    latency = trace.get("latency") or {}
    tool_trace = trace.get("tool_trace") or []
    key = str(turn.get("turn_id") or fallback_key)

    prompt_shape = _prompt_safe_shape(trace)
    return ReplayTurnSnapshot(
        key=key,
        prompt_fingerprint=_stable_hash(prompt_shape),
        tool_decision=tuple(
            f"{item.get('name')}:{bool(item.get('ok'))}:{item.get('error_code') or ''}"
            for item in tool_trace
        ),
        first_delta_ms=_int_or_none(latency.get("first_delta_ms")),
        total_ms=_int_or_none(latency.get("total_ms")),
    )


def snapshots_from_artifact(data: Any) -> list[ReplayTurnSnapshot]:
    """Decode a replay artifact.

    Accepted shapes:
      - ``{"turns": [...]}``
      - ``[...]``

    Each turn may be a raw ``turn_trace``, ``{"turn_trace": ...}``, or
    ``{"metadata": {"turn_trace": ...}}``. Already-normalized snapshots are
    also accepted for script/unit-test convenience.
    """
    raw_turns = data.get("turns", data) if isinstance(data, dict) else data
    if not isinstance(raw_turns, list):
        raise ValueError("replay artifact must be a list or {'turns': [...]}")
    snapshots: list[ReplayTurnSnapshot] = []
    for idx, item in enumerate(raw_turns):
        if not isinstance(item, dict):
            raise ValueError(f"turn {idx} must be an object")
        if "prompt_fingerprint" in item:
            snapshots.append(
                ReplayTurnSnapshot(
                    key=str(item.get("key") or idx),
                    prompt_fingerprint=str(item["prompt_fingerprint"]),
                    tool_decision=tuple(item.get("tool_decision") or ()),
                    first_delta_ms=_int_or_none(item.get("first_delta_ms")),
                    total_ms=_int_or_none(item.get("total_ms")),
                )
            )
            continue
        trace = item.get("turn_trace") or (item.get("metadata") or {}).get("turn_trace") or item
        snapshots.append(snapshot_from_turn_trace(trace, fallback_key=str(idx)))
    return snapshots


def compare_replay_snapshots(
    baseline: list[ReplayTurnSnapshot],
    candidate: list[ReplayTurnSnapshot],
    *,
    thresholds: ReplayDiffThresholds | None = None,
) -> ReplayGateReport:
    thresholds = thresholds or ReplayDiffThresholds()
    total = min(len(baseline), len(candidate))
    prompt_changed: list[dict[str, Any]] = []
    tool_changed: list[dict[str, Any]] = []
    latency_regressions: list[ReplayLatencyRegression] = []

    for idx in range(total):
        base = baseline[idx]
        cand = candidate[idx]
        key = cand.key or base.key or str(idx)
        if base.prompt_fingerprint != cand.prompt_fingerprint:
            prompt_changed.append(
                {
                    "index": idx,
                    "key": key,
                    "baseline": base.prompt_fingerprint,
                    "candidate": cand.prompt_fingerprint,
                }
            )
        if base.tool_decision != cand.tool_decision:
            tool_changed.append(
                {
                    "index": idx,
                    "key": key,
                    "baseline": list(base.tool_decision),
                    "candidate": list(cand.tool_decision),
                }
            )
        for field in ("first_delta_ms", "total_ms"):
            regression = _latency_regression(idx, key, field, base, cand, thresholds)
            if regression is not None:
                latency_regressions.append(regression)

    missing = max(0, len(baseline) - len(candidate))
    extra = max(0, len(candidate) - len(baseline))
    denom = max(total, 1)
    prompt_ratio = len(prompt_changed) / denom
    tool_ratio = len(tool_changed) / denom
    passed = (
        missing == 0
        and extra == 0
        and prompt_ratio <= thresholds.max_prompt_change_ratio
        and tool_ratio <= thresholds.max_tool_decision_change_ratio
        and not latency_regressions
    )
    return ReplayGateReport(
        passed=passed,
        total_compared=total,
        thresholds=thresholds,
        prompt_changed=prompt_changed,
        tool_decision_changed=tool_changed,
        latency_regressions=latency_regressions,
        missing_turns=missing,
        extra_turns=extra,
    )


def _latency_regression(
    index: int,
    key: str,
    field: str,
    baseline: ReplayTurnSnapshot,
    candidate: ReplayTurnSnapshot,
    thresholds: ReplayDiffThresholds,
) -> ReplayLatencyRegression | None:
    base = getattr(baseline, field)
    cand = getattr(candidate, field)
    if base is None or cand is None or cand <= base:
        return None
    delta = cand - base
    ratio = delta / max(base, 1)
    if delta <= thresholds.max_latency_regression_ms:
        return None
    if ratio <= thresholds.max_latency_regression_ratio:
        return None
    return ReplayLatencyRegression(
        index=index,
        key=key,
        field=field,
        baseline_ms=base,
        candidate_ms=cand,
        delta_ms=delta,
        delta_ratio=ratio,
    )


def _prompt_safe_shape(trace: dict[str, Any]) -> dict[str, Any]:
    ledger = trace.get("context_ledger") or {}
    memory = trace.get("memory_trace") or {}
    privacy = trace.get("privacy") or {}
    turn = trace.get("turn") or {}
    segments = ledger.get("segments") or []
    return {
        "schema_version": trace.get("schema_version"),
        "triage": turn.get("triage"),
        "trigger": turn.get("trigger"),
        "context": [
            {
                "kind": seg.get("kind"),
                "source": seg.get("source"),
                "token_bucket": _token_bucket(seg.get("token_estimate")),
                "dropped": bool(seg.get("dropped")),
                "degraded": bool((seg.get("metadata") or {}).get("degraded")),
            }
            for seg in segments
        ],
        "degraded_sources": sorted(ledger.get("degraded_sources") or []),
        "memory": {
            "attempted": bool(memory.get("attempted")),
            "skipped_reason": memory.get("skipped_reason"),
            "degraded": bool(memory.get("degraded")),
            "hit_count": memory.get("hit_count") or 0,
            "context_injected": bool(memory.get("context_injected")),
        },
        "privacy_mode": privacy.get("mode"),
    }


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _token_bucket(value: Any) -> int | None:
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return 0
    return ((tokens - 1) // 50 + 1) * 50


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ReplayDiffThresholds",
    "ReplayGateReport",
    "ReplayLatencyRegression",
    "ReplayTurnSnapshot",
    "compare_replay_snapshots",
    "snapshot_from_turn_trace",
    "snapshots_from_artifact",
]
