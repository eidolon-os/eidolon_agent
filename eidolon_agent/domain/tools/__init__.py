"""Tool system: registry, dispatcher, built-ins."""

from eidolon_agent.domain.tools.builtin import EmitEventTool, GetTimeTool
from eidolon_agent.domain.tools.dispatcher import ToolDispatcher
from eidolon_agent.domain.tools.registry import ToolRegistry

__all__ = ["EmitEventTool", "GetTimeTool", "ToolDispatcher", "ToolRegistry"]
