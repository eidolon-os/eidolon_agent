"""Registry of all registered :class:`ToolPort` instances."""

from __future__ import annotations

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.core.ports.tool import ToolPort
from eidolon_agent.core.types.tool import ToolSchema


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolPort] = {}

    def register(self, tool: ToolPort) -> None:
        name = tool.schema.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = tool

    def deregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> ToolPort:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise NotFoundError(f"tool not registered: {name}") from exc

    def list_schemas(self) -> list[ToolSchema]:
        return [t.schema for t in self._tools.values()]

    def names(self) -> list[str]:
        return sorted(self._tools)
