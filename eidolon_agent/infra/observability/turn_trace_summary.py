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
    memory_write = trace.get("memory_write_trace") or {}
    tools = trace.get("tool_trace") or []
    privacy = trace.get("privacy") or {}
    latency = trace.get("latency") or {}
    development_guards = trace.get("development_guards") or {}
    harness = trace.get("harness") or {}
    snapshot = snapshot_from_turn_trace(trace, fallback_key="turn")

    return {
        "schema_version": trace.get("schema_version"),
        "privacy_mode": privacy.get("mode"),
        "prompt_fingerprint": snapshot.prompt_fingerprint,
        "context": _context_summary(ledger),
        "context_structure_version": trace.get("context_structure_version"),
        "history_presentation": trace.get("history_presentation"),
        "context_tags": list(trace.get("context_tags") or []),
        "interrupted_context_dropped_count": trace.get(
            "interrupted_context_dropped_count"
        )
        or 0,
        "stale_generation_dropped": trace.get("stale_generation_dropped") or 0,
        "memory": {
            "attempted": bool(memory.get("attempted")),
            "degraded": bool(memory.get("degraded")),
            "degraded_reason": memory.get("degraded_reason"),
            "skipped_reason": memory.get("skipped_reason"),
            "hit_count": memory.get("hit_count") or 0,
            "kg_triple_count": memory.get("kg_triple_count") or 0,
            "context_injected": bool(memory.get("context_injected")),
        },
        "memory_write": {
            "disposition": memory_write.get("disposition"),
            "reason": memory_write.get("reason"),
            "fanout_allowed": bool(memory_write.get("fanout_allowed")),
            "skipped_reason": memory_write.get("skipped_reason"),
            "policy_version": memory_write.get("policy_version"),
        },
        "tools": {
            "count": len(tools),
            "names": [t.get("name") for t in tools if t.get("name")],
            "error_count": sum(1 for t in tools if not bool(t.get("ok"))),
            "cached_count": sum(1 for t in tools if bool(t.get("cached"))),
            "total_latency_ms": sum(_int_or_zero(t.get("latency_ms")) for t in tools),
            "repeat_suppressed_count": trace.get("tool_repeat_suppressed_count") or 0,
        },
        "harness": _harness_summary(harness),
        "latency": {
            "guard_ms": latency.get("guard_ms"),
            "triage_ms": latency.get("triage_ms"),
            "compile_ms": latency.get("compile_ms"),
            "first_delta_ms": latency.get("first_delta_ms", latency_first_delta_ms),
            "output_ms": latency.get("output_ms"),
            "tool_ms": latency.get("tool_ms", 0),
            "total_ms": latency.get("total_ms", total_latency_ms),
        },
        "development_guards": _development_guard_summary(development_guards),
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


def _development_guard_summary(guards: dict[str, Any]) -> dict[str, Any]:
    context = guards.get("context_budget") or {}
    memory_write = guards.get("memory_write_policy") or {}
    tool = guards.get("tool_policy") or {}
    tool_schema = guards.get("tool_schema_budget") or {}
    return {
        "context_budget": {
            "mode": context.get("mode"),
            "applied": bool(context.get("applied")),
            "max_tokens": context.get("max_tokens"),
            "protected_token_estimate": context.get("protected_token_estimate") or 0,
            "optional_token_estimate": context.get("optional_token_estimate") or 0,
            "output_reserve_tokens": context.get("output_reserve_tokens"),
            "dropped_count": context.get("dropped_count") or 0,
            "shadow_dropped_count": context.get("shadow_dropped_count") or 0,
            "shadow_dropped_kinds": list(context.get("shadow_dropped_kinds") or []),
        },
        "tool_schema_budget": {
            "schema_count": tool_schema.get("schema_count") or 0,
            "schema_token_estimate": tool_schema.get("schema_token_estimate") or 0,
            "schema_budget_tokens": tool_schema.get("schema_budget_tokens"),
            "schema_budget_exceeded": bool(
                tool_schema.get("schema_budget_exceeded")
            ),
        },
        "memory_write_policy": {
            "mode": memory_write.get("mode"),
            "shadow_only": bool(memory_write.get("shadow_only")),
            "fanout_allowed": bool(memory_write.get("fanout_allowed")),
            "skipped_reason": memory_write.get("skipped_reason"),
            "disposition": memory_write.get("disposition"),
        },
        "tool_policy": {
            "schema_strict": bool(tool.get("schema_strict")),
            "require_idempotency_for_side_effect_tools": bool(
                tool.get("require_idempotency_for_side_effect_tools")
            ),
            "max_tool_iters": tool.get("max_tool_iters"),
        },
    }


def _harness_summary(harness: dict[str, Any]) -> dict[str, Any]:
    tools = harness.get("tools") or {}
    handoffs = harness.get("handoffs") or []
    return {
        "kind": harness.get("kind"),
        "segment_kinds": list(harness.get("segment_kinds") or []),
        "visible_tool_names": list(tools.get("visible_names") or []),
        "handoff_count": len(handoffs),
        "handoff_tool_names": [
            item.get("tool_name") for item in handoffs if item.get("tool_name")
        ],
    }


__all__ = ["build_turn_observability_summary"]
