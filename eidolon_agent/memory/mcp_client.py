"""MCP Streamable-HTTP client pool — one session per user_id (port).

eidolon-memory binds each agent_runner to a port; we hold one long-lived MCP
session per user. ``recall_context`` is the hot path; ``search`` etc. are
exposed for admin / debugging.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from eidolon_agent.core.errors import MemoryUnavailableError

_log = logging.getLogger(__name__)


class McpUserSession:
    """One MCP HTTP session for one user. Lazy-connected, reusable."""

    def __init__(self, mcp_url: str, *, bearer_token: str | None = None) -> None:
        self._url = mcp_url
        self._token = bearer_token
        self._session = None
        self._client_cm = None
        self._lock = asyncio.Lock()

    async def _ensure(self):  # type: ignore[no-untyped-def]
        if self._session is not None:
            return self._session
        async with self._lock:
            if self._session is not None:
                return self._session
            try:
                from mcp.client.session import ClientSession
                from mcp.client.streamable_http import streamablehttp_client
            except ImportError as exc:
                raise MemoryUnavailableError("mcp client not installed") from exc

            headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
            self._client_cm = streamablehttp_client(self._url, headers=headers)
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
        # MCP returns a CallToolResult with content blocks; we extract JSON text if present.
        for block in result.content or []:
            if getattr(block, "type", None) == "text":
                try:
                    return json.loads(block.text)  # type: ignore[attr-defined]
                except (json.JSONDecodeError, AttributeError):
                    return {"raw": getattr(block, "text", "")}
        return {}

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


class McpClientPool:
    """user_id → :class:`McpUserSession`. One session per user, lazy."""

    def __init__(self, *, endpoints: dict[str, str], bearer_tokens: dict[str, str] | None = None) -> None:
        self._endpoints = endpoints
        self._tokens = bearer_tokens or {}
        self._sessions: dict[str, McpUserSession] = {}
        self._lock = asyncio.Lock()

    async def session_for(self, user_id: str) -> McpUserSession:
        async with self._lock:
            sess = self._sessions.get(user_id)
            if sess is None:
                url = self._endpoints.get(user_id)
                if url is None:
                    raise MemoryUnavailableError(f"no MCP endpoint for user {user_id}")
                sess = McpUserSession(url, bearer_token=self._tokens.get(user_id))
                self._sessions[user_id] = sess
            return sess

    async def close_all(self) -> None:
        async with self._lock:
            for s in self._sessions.values():
                await s.close()
            self._sessions.clear()

    async def health(self) -> bool:
        # Cheap: succeeds if any endpoint configured. Real check would ping MCP.
        return bool(self._endpoints)


@asynccontextmanager
async def transient_mcp_session(mcp_url: str, *, bearer_token: str | None = None):
    """Used by admin one-shots that don't warrant a long-lived session."""
    sess = McpUserSession(mcp_url, bearer_token=bearer_token)
    try:
        yield sess
    finally:
        await sess.close()
