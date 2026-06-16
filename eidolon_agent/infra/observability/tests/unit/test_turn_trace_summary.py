"""Admin turn trace summary."""

from __future__ import annotations

import pytest

from eidolon_agent.infra.observability.turn_trace_summary import (
    build_turn_observability_summary,
)

pytestmark = pytest.mark.unit


def test_summary_is_prompt_safe_and_operator_friendly() -> None:
    metadata = {
        "turn_trace": {
            "schema_version": "turn_trace.v1",
            "turn": {"turn_id": "t1", "trigger": "user_utterance", "triage": "simple"},
            "latency": {"compile_ms": 12, "first_delta_ms": 80, "total_ms": 160},
            "context_ledger": {
                "segments": [
                    {
                        "kind": "persona",
                        "source": "personas_service",
                        "token_estimate": 100,
                        "content": "secret prompt text",
                    }
                ],
                "dropped_segments": [
                    {
                        "kind": "history",
                        "source": "history_manager",
                        "token_estimate": 80,
                        "reason": "token_budget_exceeded",
                    }
                ],
                "degraded_sources": [],
                "total_token_estimate": 100,
            },
            "memory_trace": {
                "attempted": True,
                "degraded": False,
                "degraded_reason": None,
                "hit_count": 2,
                "kg_triple_count": 3,
                "context_injected": True,
            },
            "memory_write_trace": {
                "disposition": "semantic_upsert",
                "reason": "stable_preference_or_identity",
                "fanout_allowed": True,
                "skipped_reason": None,
                "policy_version": "agent_memory_policy.v1",
            },
            "tool_trace": [
                {
                    "name": "get_time",
                    "ok": True,
                    "latency_ms": 4,
                    "cached": True,
                },
                {
                    "name": "emit_event",
                    "ok": False,
                    "error_code": "eidolon.tool_permission_denied",
                    "latency_ms": 2,
                },
            ],
            "privacy": {"mode": "normal"},
            "harness": {
                "kind": "realtime_agent_harness",
                "segment_kinds": ["persona", "harness_policy", "current_user"],
                "tools": {"visible_names": ["get_time", "delegate_to_coworker"]},
                "handoffs": [
                    {
                        "tool_name": "delegate_to_coworker",
                        "task_id": "task-1",
                        "accepted": True,
                    }
                ],
            },
            "development_guards": {
                "context_budget": {
                    "mode": "enabled",
                    "applied": True,
                    "max_tokens": 6000,
                    "dropped_count": 1,
                    "shadow_dropped_count": 1,
                    "shadow_dropped_kinds": ["history"],
                },
                "memory_write_policy": {
                    "mode": "enabled",
                    "shadow_only": False,
                    "fanout_allowed": True,
                    "skipped_reason": None,
                    "disposition": "semantic_upsert",
                },
                "tool_policy": {
                    "schema_strict": True,
                    "require_idempotency_for_side_effect_tools": False,
                    "max_tool_iters": 4,
                },
            },
        }
    }

    summary = build_turn_observability_summary(metadata)

    assert summary is not None
    assert summary["context"]["segment_kinds"] == ["persona"]
    assert summary["context"]["dropped_kinds"] == ["history"]
    assert summary["memory"]["hit_count"] == 2
    assert summary["memory"]["kg_triple_count"] == 3
    assert summary["memory"]["degraded_reason"] is None
    assert summary["memory_write"]["disposition"] == "semantic_upsert"
    assert summary["tools"]["count"] == 2
    assert summary["tools"]["error_count"] == 1
    assert summary["tools"]["cached_count"] == 1
    assert summary["tools"]["total_latency_ms"] == 6
    assert summary["harness"]["kind"] == "realtime_agent_harness"
    assert summary["harness"]["segment_kinds"] == [
        "persona",
        "harness_policy",
        "current_user",
    ]
    assert summary["harness"]["visible_tool_names"] == [
        "get_time",
        "delegate_to_coworker",
    ]
    assert summary["harness"]["handoff_count"] == 1
    assert summary["harness"]["handoff_tool_names"] == ["delegate_to_coworker"]
    assert summary["latency"]["compile_ms"] == 12
    assert summary["development_guards"]["context_budget"]["mode"] == "enabled"
    assert summary["development_guards"]["context_budget"]["dropped_count"] == 1
    assert summary["development_guards"]["memory_write_policy"]["fanout_allowed"] is True
    assert summary["development_guards"]["tool_policy"]["schema_strict"] is True
    assert "secret prompt text" not in str(summary)


def test_summary_returns_none_without_turn_trace() -> None:
    assert build_turn_observability_summary({"x": 1}) is None
