"""Complex-task dispatch to the external workstation agent."""

from eidolon_agent.dispatch.classifier import TaskClassifier
from eidolon_agent.dispatch.workstation_client import NatsWorkstationClient

__all__ = ["NatsWorkstationClient", "TaskClassifier"]
