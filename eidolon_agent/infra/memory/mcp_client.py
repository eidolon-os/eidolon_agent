"""MCP Streamable-HTTP client pool — one read pool per Memory Realm.

eidolon-memory binds each agent_runner to a port. ``recall_context`` is the
Agent hot path, so every bounded reader transport is connected before the
Agent advertises readiness instead of charging MCP negotiation to a user's
first turn.
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

_DEFAULT_READ_SESSION_POOL_SIZE = 4


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
            except BaseException:
                await self.close()
                raise

    async def call_tool(self, name: str, arguments: dict) -> dict:
        try:
            session = await self._ensure()
            result = await session.call_tool(name, arguments)
        except MemoryUnavailableError:
            raise
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
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:
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
        session = self._session
        client_cm = self._client_cm
        http_client = self._http_client
        self._session = None
        self._client_cm = None
        self._http_client = None
        if session is not None:
            try:
                await session.__aexit__(None, None, None)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                pass
        if client_cm is not None:
            try:
                await client_cm.__aexit__(None, None, None)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                pass
        if http_client is not None:
            try:
                await http_client.aclose()
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:
                pass
        # Re-probe capabilities after a reconnect (server may have changed).
        self._tool_names = None


class McpReadSessionPool:
    """A small set of independent MCP transports for one Realm.

    The MCP SDK multiplexes requests logically, but one Streamable-HTTP
    session still becomes a head-of-line queue under concurrent Agent turns.
    Keeping a bounded number of ordinary client sessions removes that
    transport bottleneck without introducing a reader proxy, cache, writer, or
    another consistency mechanism. Each session is owned for its entire
    lifetime by one worker task. This is required by the MCP SDK's AnyIO cancel
    scopes: a transport context must be entered, used, and exited by the same
    task. Memory remains the sole read authority and NATS remains the sole
    writer.
    """

    def __init__(
        self,
        mcp_url: str,
        *,
        bearer_token: str | None = None,
        size: int = _DEFAULT_READ_SESSION_POOL_SIZE,
    ) -> None:
        self._url = mcp_url
        self._token = bearer_token
        self._size = max(1, int(size))
        self._sessions: set[McpUserSession] = set()
        self._requests: asyncio.Queue[
            tuple[str, str | None, dict | None, asyncio.Future[Any]]
        ] = asyncio.Queue()
        self._workers: set[asyncio.Task[None]] = set()
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False
        self._tool_names: frozenset[str] | None = None

    async def _submit(
        self,
        operation: str,
        *,
        name: str | None = None,
        arguments: dict | None = None,
    ) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        async with self._lifecycle_lock:
            if self._closed:
                raise MemoryUnavailableError("MCP read session pool is closed")
            if not self._workers:
                for index in range(self._size):
                    worker = asyncio.create_task(
                        self._run_worker(),
                        name=f"mcp-read-session-{index}",
                    )
                    self._workers.add(worker)
            self._requests.put_nowait((operation, name, arguments, future))
        # The Agent's recall deadline may cancel this caller. Shielding keeps
        # cancellation from crossing into the SDK transport owner task; the
        # worker finishes or is cancelled by close() in its own task.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # There is no caller left to consume a result or exception. This
            # cancels only the hand-off Future, not the worker or MCP request.
            future.cancel()
            raise

    async def _run_worker(self) -> None:
        session = McpUserSession(self._url, bearer_token=self._token)
        self._sessions.add(session)
        try:
            while True:
                operation, name, arguments, future = await self._requests.get()
                try:
                    if operation == "call_tool":
                        result = await session.call_tool(name or "", arguments or {})
                    else:
                        result = await session.tool_names()
                    if not future.done():
                        future.set_result(result)
                except asyncio.CancelledError:
                    if not future.done():
                        future.set_exception(
                            MemoryUnavailableError("MCP read session pool is closed")
                        )
                    raise
                except BaseException as exc:
                    if not future.done():
                        future.set_exception(exc)
                finally:
                    self._requests.task_done()
        finally:
            # Enter/use/exit remain in this worker task, satisfying AnyIO's
            # cancel-scope ownership contract even during overload shutdown.
            await session.close()
            self._sessions.discard(session)

    async def call_tool(self, name: str, arguments: dict) -> dict:
        result = await self._submit("call_tool", name=name, arguments=arguments)
        return result

    async def tool_names(self) -> frozenset[str] | None:
        if self._tool_names is not None:
            return self._tool_names
        names = await self._submit("tool_names")
        if names is not None:
            self._tool_names = names
        return names

    async def supports(self, name: str) -> bool:
        names = await self.tool_names()
        return True if names is None else name in names

    async def warmup(self) -> None:
        """Open every bounded reader transport and negotiate capabilities."""

        results = await asyncio.gather(
            *(self._submit("tool_names") for _ in range(self._size))
        )
        names = next((value for value in results if value is not None), None)
        if names is not None:
            self._tool_names = names

    def matches(self, *, mcp_url: str, bearer_token: str | None) -> bool:
        return self._url == mcp_url and self._token == bearer_token

    async def close(self) -> None:
        async with self._lifecycle_lock:
            self._closed = True
            workers = list(self._workers)
            self._workers.clear()
            self._tool_names = None
            while not self._requests.empty():
                try:
                    _operation, _name, _arguments, future = self._requests.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not future.done():
                    future.set_exception(
                        MemoryUnavailableError("MCP read session pool is closed")
                    )
                self._requests.task_done()
            for worker in workers:
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


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
    """Realm-scoped read and write MCP sessions, opened lazily.

    Read sessions use the narrow agent surface. Explicit mutations use the
    separate ops surface; keeping both sessions distinct prevents capability
    leakage while preserving connection reuse on each path.
    """

    def __init__(
        self,
        *,
        routes: MemoryRoutingTable | None = None,
        endpoints: dict[str, str] | None = None,
        bearer_tokens: dict[str, str] | None = None,
        read_session_pool_size: int = _DEFAULT_READ_SESSION_POOL_SIZE,
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
        self._read_session_pool_size = max(1, int(read_session_pool_size))
        self._sessions: dict[str, McpReadSessionPool] = {}
        self._write_sessions: dict[str, McpUserSession] = {}
        self._lock = asyncio.Lock()

    async def session_for(self, memory_space_id: str) -> McpReadSessionPool:
        return await self._session_for(memory_space_id, write=False)

    async def write_session_for(self, memory_space_id: str) -> McpUserSession:
        return await self._session_for(memory_space_id, write=True)

    async def warmup_read_sessions(self) -> None:
        """Warm all currently reachable Realm readers without issuing a query."""

        memory_space_ids = await self._routes.memory_space_ids()

        async def warm_one(memory_space_id: str) -> None:
            session = await self.session_for(memory_space_id)
            await session.warmup()

        await asyncio.gather(*(warm_one(value) for value in memory_space_ids))

    async def _session_for(
        self,
        memory_space_id: str,
        *,
        write: bool,
    ) -> McpReadSessionPool | McpUserSession:
        route, unavailable_reason = await self._routes.route_status_for(memory_space_id)
        if route is None:
            if write:
                await self.drop_write_session(memory_space_id)
            else:
                await self.drop_session(memory_space_id)
            raise MemoryUnavailableError(
                f"no reachable MCP endpoint for memory space {memory_space_id}: {unavailable_reason}",
                details={"memory_space_id": memory_space_id, "reason": unavailable_reason},
            )
        mcp_url = (route.ops_mcp_url or route.mcp_url) if write else route.mcp_url
        sessions = self._write_sessions if write else self._sessions
        async with self._lock:
            sess = sessions.get(memory_space_id)
            if sess is not None and sess.matches(
                mcp_url=mcp_url,
                bearer_token=route.bearer_token,
            ):
                return sess
            if sess is not None:
                await sess.close()
            sess = (
                McpUserSession(mcp_url, bearer_token=route.bearer_token)
                if write
                else McpReadSessionPool(
                    mcp_url,
                    bearer_token=route.bearer_token,
                    size=self._read_session_pool_size,
                )
            )
            sessions[memory_space_id] = sess
            return sess

    async def drop_session(
        self,
        memory_space_id: str,
        *,
        session: McpReadSessionPool | None = None,
    ) -> bool:
        """Close and remove a cached user session.

        When ``session`` is supplied, the cached object must still be that exact
        instance. This lets callers discard a poisoned session after a timeout
        without racing and closing a fresh replacement created by another turn.
        """
        return await self._drop_from(
            self._sessions,
            memory_space_id,
            session=session,
        )

    async def drop_write_session(
        self,
        memory_space_id: str,
        *,
        session: McpUserSession | None = None,
    ) -> bool:
        return await self._drop_from(
            self._write_sessions,
            memory_space_id,
            session=session,
        )

    async def _drop_from(
        self,
        sessions: dict[str, McpReadSessionPool] | dict[str, McpUserSession],
        memory_space_id: str,
        *,
        session: McpReadSessionPool | McpUserSession | None,
    ) -> bool:
        async with self._lock:
            cached = sessions.get(memory_space_id)
            if cached is None:
                return False
            if session is not None and cached is not session:
                return False
            sessions.pop(memory_space_id, None)
        await cached.close()
        return True

    async def close_all(self) -> None:
        async with self._lock:
            sessions = [*self._sessions.values(), *self._write_sessions.values()]
            self._sessions.clear()
            self._write_sessions.clear()
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
