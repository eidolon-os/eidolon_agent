"""Complex-task dispatch to the external workstation agent."""

from eidolon_agent.domain.dispatch.classifier import TaskClassifier
from eidolon_agent.domain.dispatch.workstation_client import NatsWorkstationClient

__all__ = ["NatsWorkstationClient", "TaskClassifier"]
