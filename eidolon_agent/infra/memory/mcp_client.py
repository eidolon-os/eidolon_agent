"""MCP Streamable-HTTP client pool — one session per memory_space_id (port).

eidolon-memory binds each agent_runner to a port; we hold one long-lived MCP
session per user. ``recall_context`` is the hot path; ``search`` etc. are
exposed for admin / debugging.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx

from eidolon_agent.core.errors import MemoryUnavailableError
from eidolon_agent.infra.memory.discovery import MemoryRoutingTable

_log = logging.getLogger(__name__)


class McpUserSession:
    """One MCP HTTP session for one user. Lazy-connected, reusable."""

    def __init__(self, mcp_url: str, *, bearer_token: str | None = None) -> None:
        self._url = mcp_url
        self._token = bearer_token
        self._session = None
        self._client_cm = None
        self._http_client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        # Capability negotiation: the set of tool names the server advertises,
        # probed once via list_tools and cached. None = not yet / couldn't
        # probe, which callers treat as "unknown, attempt anyway" so gating
        # never regresses behaviour when the probe itself fails.
        self._tool_names: frozenset[str] | None = None

    async def _ensure(self):  # type: ignore[no-untyped-def]
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            try:
                from mcp.client import streamable_http as streamable_http_mod
                from mcp.client.session import ClientSession
            except ImportError as exc:
                raise MemoryUnavailableError("mcp client not installed") from exc

            streamable_http_client = getattr(
                streamable_http_mod,
                "streamable_http_client",
                None,
            ) or getattr(streamable_http_mod, "streamablehttp_client", None)
            if streamable_http_client is None:
                raise MemoryUnavailableError("mcp streamable http client not available")

            headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
            if "headers" in inspect.signature(streamable_http_client).parameters:
                self._client_cm = streamable_http_client(self._url, headers=headers)
            else:
                self._http_client = httpx.AsyncClient(
                    headers=headers,
                    follow_redirects=True,
                    trust_env=False,
                )
                self._client_cm = streamable_http_client(
                    self._url,
                    http_client=self._http_client,
                )
            read, write, _ = await self._client_cm.__aenter__()
            self._session = ClientSession(read, write)
            await self._session.__aenter__()
            await self._session.initialize()
            return self._session

    async def call_tool(self, name: str, arguments: dict) -> dict:
        session = await self._ensure()
        try:
            result = await session.call_tool(name, arguments)
        except Exception as exc:
            raise MemoryUnavailableError(f"MCP call {name} failed: {exc}") from exc
        decoded = _decode_call_tool_result(result)
        if isinstance(decoded, dict):
            return decoded
        if isinstance(decoded, list):
            return {"records": decoded}
        return {"result": decoded}

    async def tool_names(self) -> frozenset[str] | None:
        """Best-effort set of tool names the server advertises (cached).

        Returns None if the server couldn't be probed; callers treat that as
        "unknown — attempt anyway" so capability negotiation is strictly an
        improvement over blind calls, never a regression.
        """
        if self._tool_names is not None:
            return self._tool_names
        try:
            session = await self._ensure()
            result = await session.list_tools()
        except Exception:
            return None
        names = frozenset(
            getattr(tool, "name", "")
            for tool in (getattr(result, "tools", None) or [])
        )
        self._tool_names = names
        return names

    async def supports(self, name: str) -> bool:
        """Whether the server advertises ``name`` (optimistic when unknown)."""
        names = await self.tool_names()
        return True if names is None else name in names

    def matches(self, *, mcp_url: str, bearer_token: str | None) -> bool:
        return self._url == mcp_url and self._token == bearer_token

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception:
                pass
            self._session = None
        if self._client_cm is not None:
            try:
                await self._client_cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._client_cm = None
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        # Re-probe capabilities after a reconnect (server may have changed).
        self._tool_names = None


def _decode_call_tool_result(result: Any) -> Any:
    if getattr(result, "isError", False):
        parts: list[str] = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        raise MemoryUnavailableError("MCP tool error: " + (" | ".join(parts) or "unknown"))

    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return _unwrap_fastmcp_result(structured)

    # MCP returns a CallToolResult with content blocks; extract JSON text if present.
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            try:
                return _unwrap_fastmcp_result(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                return {"raw": text}
    return {}


def _unwrap_fastmcp_result(payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload) == {"result"}:
        return payload["result"]
    return payload


class McpClientPool:
    """memory_space_id -> :class:`McpUserSession`. One session per space, lazy."""

    def __init__(
        self,
        *,
        routes: MemoryRoutingTable | None = None,
        endpoints: dict[str, str] | None = None,
        bearer_tokens: dict[str, str] | None = None,
    ) -> None:
        if routes is None:
            from eidolon_agent.config.settings import MemoryEndpoint, NatsSettings

            routes = MemoryRoutingTable.from_static(
                endpoints=[
                    MemoryEndpoint(
                        memory_space_id=memory_space_id,
                        mcp_url=mcp_url,
                        bearer_token=(bearer_tokens or {}).get(memory_space_id),
                    )
                    for memory_space_id, mcp_url in (endpoints or {}).items()
                ],
                nats=NatsSettings(),
            )
        self._routes = routes
        self._sessions: dict[str, McpUserSession] = {}
        self._lock = asyncio.Lock()

    async def session_for(self, memory_space_id: str) -> McpUserSession:
        route, unavailable_reason = await self._routes.route_status_for(memory_space_id)
        if route is None:
            await self.drop_session(memory_space_id)
            raise MemoryUnavailableError(
                f"no reachable MCP endpoint for memory space {memory_space_id}: {unavailable_reason}",
                details={"memory_space_id": memory_space_id, "reason": unavailable_reason},
            )
        async with self._lock:
            sess = self._sessions.get(memory_space_id)
            if sess is not None and sess.matches(
                mcp_url=route.mcp_url,
                bearer_token=route.bearer_token,
            ):
                return sess
            if sess is not None:
                await sess.close()
            sess = McpUserSession(route.mcp_url, bearer_token=route.bearer_token)
            self._sessions[memory_space_id] = sess
            return sess

    async def drop_session(
        self,
        memory_space_id: str,
        *,
        session: McpUserSession | None = None,
    ) -> bool:
        """Close and remove a cached user session.

        When ``session`` is supplied, the cached object must still be that exact
        instance. This lets callers discard a poisoned session after a timeout
        without racing and closing a fresh replacement created by another turn.
        """
        async with self._lock:
            sess = self._sessions.get(memory_space_id)
            if sess is None:
                return False
            if session is not None and sess is not session:
                return False
            self._sessions.pop(memory_space_id, None)
        if sess is not None:
            await sess.close()
        return True

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            await s.close()

    async def health(self) -> bool:
        return await self._routes.endpoint_count() > 0


@asynccontextmanager
async def transient_mcp_session(mcp_url: str, *, bearer_token: str | None = None):
    """Used by admin one-shots that don't warrant a long-lived session."""
    sess = McpUserSession(mcp_url, bearer_token=bearer_token)
    try:
        yield sess
    finally:
        await sess.close()
