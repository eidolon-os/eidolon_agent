"""Tool system: registry, dispatcher, built-ins."""

from eidolon_agent.domain.tools.builtin import (
    EmitEventTool,
    GetTimeTool,
    GetWeatherTool,
    SubmitLongTaskTool,
)
from eidolon_agent.domain.tools.dispatcher import ToolDispatcher
from eidolon_agent.domain.tools.registry import ToolRegistry

__all__ = [
    "EmitEventTool",
    "GetTimeTool",
    "GetWeatherTool",
    "SubmitLongTaskTool",
    "ToolDispatcher",
    "ToolRegistry",
]
