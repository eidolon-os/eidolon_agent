"""Prometheus metric registry — defines all SLO + capacity + business metrics."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

registry = CollectorRegistry()

turn_latency_first_delta = Histogram(
    "turn_latency_first_delta_seconds",
    "Time from Turn start to first DELTA event.",
    buckets=(0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 2.0, 5.0),
    registry=registry,
)
turn_latency_total = Histogram(
    "turn_latency_total_seconds",
    "Wall-clock duration of a Turn.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
    registry=registry,
)
turn_error_total = Counter(
    "turn_error_total",
    "Turns that ended in ERROR.",
    labelnames=("code",),
    registry=registry,
)
triage_decision_total = Counter(
    "triage_decision_total",
    "Triage classifier outcomes.",
    labelnames=("kind",),
    registry=registry,
)
memory_recall_degraded_total = Counter(
    "memory_recall_degraded_total",
    "Memory recall returned degraded result.",
    registry=registry,
)
dispatch_handoff_total = Counter(
    "dispatch_handoff_total",
    "Complex tasks handed off to workstation.",
    registry=registry,
)
grpc_active_streams = Gauge(
    "grpc_active_streams",
    "Currently open Chat bidi streams.",
    registry=registry,
)
agent_instances_total = Gauge(
    "agent_instances_total",
    "Active AgentInstances.",
    labelnames=("template_id",),
    registry=registry,
)
guardrail_triggered_total = Counter(
    "guardrail_triggered_total",
    "Guardrail interventions.",
    labelnames=("category",),
    registry=registry,
)
