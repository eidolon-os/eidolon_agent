"""Logging / metrics / tracing scaffolding."""

from eidolon_agent.infra.observability.logging import configure_logging
from eidolon_agent.infra.observability.live_turns import LiveTurnBoard, LiveTurnView
from eidolon_agent.infra.observability.turn_trace_summary import (
    build_live_turn_observability_summary,
    build_turn_observability_summary,
)

__all__ = [
    "LiveTurnBoard",
    "LiveTurnView",
    "build_live_turn_observability_summary",
    "build_turn_observability_summary",
    "configure_logging",
]
