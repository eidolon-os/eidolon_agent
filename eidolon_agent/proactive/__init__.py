"""Proactive engine — sources push potential triggers, engine evaluates, throttler gates."""

from eidolon_agent.proactive.engine import ProactiveEngine
from eidolon_agent.proactive.throttler import ProactiveThrottler

__all__ = ["ProactiveEngine", "ProactiveThrottler"]
