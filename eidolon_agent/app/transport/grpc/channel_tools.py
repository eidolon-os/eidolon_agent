from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from eidolon_agent.core.ports.channel_tool import (
    ChannelToolRequest,
    ChannelToolResult,
)
from eidolon_agent.core.types.tool import ToolResult

logger = logging.getLogger("eidolon_agent.grpc.channel_tools")

ChannelToolSender = Callable[[ChannelToolRequest], Awaitable[None]]
ChannelToolCancelSender = Callable[[str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _PendingChannelTool:
    digest: str
    turn_id: str
    name: str
    future: asyncio.Future[ChannelToolResult]


class ChannelToolExchange:
    def __init__(
        self,
        send: ChannelToolSender,
        cancel: ChannelToolCancelSender,
        *,
        timeout_s: float = 65.0,
    ) -> None:
        self._send = send
        self._cancel = cancel
        self._timeout_s = timeout_s
        self._pending: dict[tuple[str, str], _PendingChannelTool] = {}
        self._completed: dict[tuple[str, str], tuple[str, ToolResult]] = {}

    async def execute(self, request: ChannelToolRequest) -> ToolResult:
        key = (request.turn_id, request.call_id)
        digest = _request_digest(request)
        completed = self._completed.get(key)
        if completed is not None:
            if completed[0] != digest:
                return _replay_error(request)
            return completed[1]
        pending = self._pending.get(key)
        if pending is not None:
            if pending.digest != digest:
                return _replay_error(request)
            result = await asyncio.wait_for(asyncio.shield(pending.future), self._timeout_s)
            return _tool_result(result)

        future = asyncio.get_running_loop().create_future()
        self._pending[key] = _PendingChannelTool(
            digest=digest,
            turn_id=request.turn_id,
            name=request.name,
            future=future,
        )
        try:
            await self._send(request)
            result = await asyncio.wait_for(future, self._timeout_s)
            tool_result = _tool_result(result)
            self._completed[key] = (digest, tool_result)
            logger.info(
                "channel_tool_exchange response turn_id=%s call_id=%s name=%s ok=%s error_code=%s",
                request.turn_id,
                request.call_id,
                request.name,
                tool_result.ok,
                tool_result.error_code or "-",
            )
            return tool_result
        except asyncio.CancelledError:
            await self._cancel_safely(request.turn_id, request.call_id)
            raise
        except TimeoutError:
            await self._cancel_safely(request.turn_id, request.call_id)
            tool_result = ToolResult(
                call_id=request.call_id,
                name=request.name,
                ok=False,
                error_code="CHANNEL_TOOL_TIMEOUT",
                error_message=f"Channel tool exceeded {self._timeout_s:.0f}s",
            )
            self._completed[key] = (digest, tool_result)
            logger.warning(
                "channel_tool_exchange timed out turn_id=%s call_id=%s name=%s error_code=%s",
                request.turn_id,
                request.call_id,
                request.name,
                tool_result.error_code,
            )
            return tool_result
        finally:
            self._pending.pop(key, None)

    async def _cancel_safely(self, turn_id: str, call_id: str) -> None:
        try:
            await self._cancel(turn_id, call_id)
        except Exception:
            logger.warning(
                "channel tool cancel could not be delivered turn_id=%s call_id=%s", turn_id, call_id
            )

    def accept(self, result: ChannelToolResult) -> bool:
        pending = self._pending.get((result.turn_id, result.call_id))
        if pending is None or pending.future.done():
            return False
        if (pending.turn_id, pending.name) != (result.turn_id, result.name):
            return False
        pending.future.set_result(result)
        return True


def _request_digest(request: ChannelToolRequest) -> str:
    payload = json.dumps(
        {"turn_id": request.turn_id, "name": request.name, "arguments": request.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _tool_result(result: ChannelToolResult) -> ToolResult:
    return ToolResult(
        call_id=result.call_id,
        name=result.name,
        ok=result.ok,
        content=result.content,
        error_code=result.error_code or None,
        error_message=result.error_message or None,
    )


def _replay_error(request: ChannelToolRequest) -> ToolResult:
    return ToolResult(
        call_id=request.call_id,
        name=request.name,
        ok=False,
        error_code="CHANNEL_TOOL_CALL_REPLAY",
        error_message="call_id was reused with different request content",
    )
