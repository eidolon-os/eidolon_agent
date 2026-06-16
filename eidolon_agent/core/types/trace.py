"""Brain-side turn trace metadata.

The trace is persisted inside ``TurnRow.metadata`` so admin/replay tools can
explain a turn without reconstructing prompts or reading private message text.
It is deliberately a compact JSON contract, not a full event log: channel owns
end-to-end voice timelines, memory owns recall/write quality, and agent owns
brain orchestration facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TRACE_SCHEMA_VERSION = "turn_trace.v1"


@dataclass(frozen=True, slots=True)
class LatencyBreakdown:
    guard_ms: int | None = None
    triage_ms: int | None = None
    compile_ms: int | None = None
    first_delta_ms: int | None = None
    output_ms: int | None = None
    tool_ms: int = 0
    total_ms: int | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "guard_ms": self.guard_ms,
            "triage_ms": self.triage_ms,
            "compile_ms": self.compile_ms,
            "first_delta_ms": self.first_delta_ms,
            "output_ms": self.output_ms,
            "tool_ms": self.tool_ms,
            "total_ms": self.total_ms,
        }


@dataclass(frozen=True, slots=True)
class ToolTrace:
    call_id: str
    name: str
    ok: bool
    latency_ms: int
    error_code: str | None = None
    cached: bool = False

    def to_metadata(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "name": self.name,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "error_code": self.error_code,
            "cached": self.cached,
        }


@dataclass(frozen=True, slots=True)
class PrivacyTrace:
    mode: Literal["normal", "private", "temporary"] = "normal"
    memory_recall_allowed: bool = True
    memory_write_allowed: bool = True
    history_visible_to_context: bool = True

    def to_metadata(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "memory_recall_allowed": self.memory_recall_allowed,
            "memory_write_allowed": self.memory_write_allowed,
            "history_visible_to_context": self.history_visible_to_context,
        }


@dataclass(frozen=True, slots=True)
class PersonaTrace:
    instance_id: str | None = None
    template_id: str | None = None
    snapshot_version: int | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "template_id": self.template_id,
            "snapshot_version": self.snapshot_version,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentGuardTrace:
    """Prompt-safe development guard state for high-risk brain behavior."""

    context_budget: dict[str, Any] | None = None
    memory_write_policy: dict[str, Any] | None = None
    tool_policy: dict[str, Any] | None = None

    def to_metadata(self) -> dict[str, Any]:
        return {
            "context_budget": self.context_budget,
            "memory_write_policy": self.memory_write_policy,
            "tool_policy": self.tool_policy,
        }


@dataclass(frozen=True, slots=True)
class TurnTrace:
    turn_id: str
    conversation_id: str
    status: str
    trigger: str
    triage: str | None
    caller_kind: str | None
    model: str | None
    latency: LatencyBreakdown
    context_ledger: dict[str, Any] | None = None
    memory_trace: dict[str, Any] | None = None
    memory_write_trace: dict[str, Any] | None = None
    tool_trace: list[ToolTrace] = field(default_factory=list)
    persona: PersonaTrace = field(default_factory=PersonaTrace)
    privacy: PrivacyTrace = field(default_factory=PrivacyTrace)
    proactive_reason: dict[str, Any] | None = None
    harness_snapshot: dict[str, Any] | None = None
    development_guards: DevelopmentGuardTrace = field(
        default_factory=DevelopmentGuardTrace
    )
    usage: dict[str, int] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "boundary": "eidolon_agent.brain",
            "turn": {
                "turn_id": self.turn_id,
                "conversation_id": self.conversation_id,
                "status": self.status,
                "trigger": self.trigger,
                "triage": self.triage,
                "caller_kind": self.caller_kind,
                "model": self.model,
            },
            "latency": self.latency.to_metadata(),
            "context_ledger": self.context_ledger,
            "memory_trace": self.memory_trace,
            "memory_write_trace": self.memory_write_trace,
            "tool_trace": [t.to_metadata() for t in self.tool_trace],
            "persona": self.persona.to_metadata(),
            "privacy": self.privacy.to_metadata(),
            "proactive_reason": self.proactive_reason,
            "harness": self.harness_snapshot,
            "development_guards": self.development_guards.to_metadata(),
            "usage": dict(self.usage),
        }


__all__ = [
    "TRACE_SCHEMA_VERSION",
    "DevelopmentGuardTrace",
    "LatencyBreakdown",
    "PersonaTrace",
    "PrivacyTrace",
    "ToolTrace",
    "TurnTrace",
]
