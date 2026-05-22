"""Proactive engine — sources push potential triggers, engine evaluates, throttler gates."""

from eidolon_agent.domain.proactive.engine import ProactiveEngine
from eidolon_agent.domain.proactive.throttler import ProactiveThrottler

__all__ = ["ProactiveEngine", "ProactiveThrottler"]
