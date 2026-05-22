"""Agent runtime — Turn pipeline, multi-template registry, triage."""

from eidolon_agent.domain.agent.companion import CompanionAgent
from eidolon_agent.domain.agent.registry import AgentInstance, AgentRegistry, AgentTemplate
from eidolon_agent.domain.agent.turn import TurnEngine

__all__ = [
    "AgentInstance",
    "AgentRegistry",
    "AgentTemplate",
    "CompanionAgent",
    "TurnEngine",
]
