"""Lifecycle hooks — pure routing layer."""

from eidolon_agent.domain.hooks.definition import CommandHook
from eidolon_agent.domain.hooks.executor import HookExecutor

__all__ = ["CommandHook", "HookExecutor"]
