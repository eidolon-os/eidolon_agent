from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from eidolon_agent.core.types.tool import ToolResult


@dataclass(frozen=True, slots=True)
class ChannelToolRequest:
    turn_id: str
    call_id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ChannelToolResult:
    turn_id: str
    call_id: str
    name: str
    ok: bool
    content: dict[str, Any]
    error_code: str = ""
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class ChannelToolCancel:
    turn_id: str
    call_id: str


class ChannelToolExecutor(Protocol):
    async def execute(self, request: ChannelToolRequest) -> ToolResult: ...
