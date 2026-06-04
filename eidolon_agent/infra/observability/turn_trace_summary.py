"""Prompt-safe summaries for admin turn observability."""

from __future__ import annotations

from typing import Any

from eidolon_agent.infra.observability.replay_diff import snapshot_from_turn_trace


def build_turn_observability_summary(
    metadata: dict[str, Any] | None,
    *,
    latency_first_delta_ms: int | None = None,
    total_latency_ms: int | None = None,
) -> dict[str, Any] | None:
    trace = (metadata or {}).get("turn_trace")
    if not isinstance(trace, dict):
        return None

    ledger = trace.get("context_ledger") or {}
    memory = trace.get("memory_trace") or {}
    tools = trace.get("tool_trace") or []
    privacy = trace.get("privacy") or {}
    latency = trace.get("latency") or {}
    snapshot = snapshot_from_turn_trace(trace, fallback_key="turn")

    return {
        "schema_version": trace.get("schema_version"),
        "privacy_mode": privacy.get("mode"),
        "prompt_fingerprint": snapshot.prompt_fingerprint,
        "context": _context_summary(ledger),
        "memory": {
            "attempted": bool(memory.get("attempted")),
            "degraded": bool(memory.get("degraded")),
            "skipped_reason": memory.get("skipped_reason"),
            "hit_count": memory.get("hit_count") or 0,
            "context_injected": bool(memory.get("context_injected")),
        },
        "tools": {
            "count": len(tools),
            "names": [t.get("name") for t in tools if t.get("name")],
            "error_count": sum(1 for t in tools if not bool(t.get("ok"))),
            "cached_count": sum(1 for t in tools if bool(t.get("cached"))),
            "total_latency_ms": sum(_int_or_zero(t.get("latency_ms")) for t in tools),
        },
        "latency": {
            "guard_ms": latency.get("guard_ms"),
            "triage_ms": latency.get("triage_ms"),
            "compile_ms": latency.get("compile_ms"),
            "first_delta_ms": latency.get("first_delta_ms", latency_first_delta_ms),
            "output_ms": latency.get("output_ms"),
            "tool_ms": latency.get("tool_ms", 0),
            "total_ms": latency.get("total_ms", total_latency_ms),
        },
    }


def _context_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    segments = ledger.get("segments") or []
    dropped = ledger.get("dropped_segments") or []
    return {
        "total_token_estimate": ledger.get("total_token_estimate") or 0,
        "segment_kinds": [s.get("kind") for s in segments if s.get("kind")],
        "dropped_count": len(dropped),
        "dropped_kinds": [s.get("kind") for s in dropped if s.get("kind")],
        "degraded_sources": list(ledger.get("degraded_sources") or []),
    }


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


__all__ = ["build_turn_observability_summary"]
