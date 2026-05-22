"""Realtime signal ring buffer (lightweight, fed by gRPC PushSignal)."""

from eidolon_agent.domain.signals.bus import SignalBus

__all__ = ["SignalBus"]
