"""McpUserSession + McpClientPool + result decoder — MCP SDK mocked.

We don't hit any real MCP server. Tests cover:
- ``_decode_call_tool_result`` for structured, JSON-text, and error blocks
- ``McpClientPool.session_for`` reuses sessions when routes are stable and
  rotates them when the route changes (URL or token)
- ``health()`` reflects route availability
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from eidolon_agent.core.errors import MemoryUnavailableError
from eidolon_agent.infra.memory.discovery import (
    MemoryNatsRoute,
    MemoryRoute,
    MemoryRoutingTable,
)
from eidolon_agent.infra.memory.mcp_client import (
    McpClientPool,
    McpReadSessionPool,
    McpUserSession,
    _decode_call_tool_result,
    transient_mcp_session,
)

pytestmark = pytest.mark.unit


# ---- result decoder ------------------------------------------------------


def test_decode_prefers_structured_content() -> None:
    r = SimpleNamespace(
        isError=False,
        structuredContent={"records": [1, 2]},
        content=[SimpleNamespace(type="text", text="ignored")],
    )
    assert _decode_call_tool_result(r) == {"records": [1, 2]}


def test_decode_unwraps_fastmcp_result_wrapper() -> None:
    r = SimpleNamespace(isError=False, structuredContent={"result": {"x": 1}})
    assert _decode_call_tool_result(r) == {"x": 1}


def test_decode_parses_json_text_block() -> None:
    r = SimpleNamespace(
        isError=False,
        structuredContent=None,
        content=[SimpleNamespace(type="text", text='{"a": 1}')],
    )
    assert _decode_call_tool_result(r) == {"a": 1}


def test_decode_non_json_text_returns_raw() -> None:
    r = SimpleNamespace(
        isError=False,
        structuredContent=None,
        content=[SimpleNamespace(type="text", text="hello")],
    )
    assert _decode_call_tool_result(r) == {"raw": "hello"}


def test_decode_error_block_raises_memory_unavailable() -> None:
    r = SimpleNamespace(
        isError=True,
        content=[SimpleNamespace(text="search timeout")],
    )
    with pytest.raises(MemoryUnavailableError, match="search timeout"):
        _decode_call_tool_result(r)


# ---- McpClientPool -------------------------------------------------------


def _routes(*routes: MemoryRoute) -> MemoryRoutingTable:
    return MemoryRoutingTable(
        nats=MemoryNatsRoute(
            url="nats://x", stream="", turn_subject_template="t", cmd_subject_template="c"
        ),
        routes={r.memory_space_id: r for r in routes},
    )


async def test_pool_health_true_when_routes_present() -> None:
    pool = McpClientPool(
        routes=_routes(MemoryRoute(memory_space_id="default.alice.default", mcp_url="http://a/mcp"))
    )
    assert await pool.health() is True


async def test_pool_health_false_when_no_routes() -> None:
    pool = McpClientPool(routes=_routes())
    assert await pool.health() is False


async def test_session_for_unknown_user_raises_unavailable() -> None:
    pool = McpClientPool(routes=_routes())
    with pytest.raises(MemoryUnavailableError, match="no_memory_route") as exc_info:
        await pool.session_for("ghost")
    assert exc_info.value.details["reason"] == "no_memory_route"


@pytest.mark.parametrize(
    ("route", "reason"),
    [
        (
            MemoryRoute(
                memory_space_id="default.alice.default", mcp_url="http://a/mcp", enabled=False
            ),
            "memory_route_disabled",
        ),
    ],
)
async def test_session_for_unavailable_route_carries_reason(
    route: MemoryRoute, reason: str
) -> None:
    pool = McpClientPool(routes=_routes(route))
    with pytest.raises(MemoryUnavailableError, match=reason) as exc_info:
        await pool.session_for("default.alice.default")
    assert exc_info.value.details == {
        "memory_space_id": "default.alice.default",
        "reason": reason,
    }


async def test_session_reused_for_same_route() -> None:
    pool = McpClientPool(
        routes=_routes(
            MemoryRoute(
                memory_space_id="default.alice.default", mcp_url="http://a/mcp", bearer_token="t1"
            )
        )
    )
    s1 = await pool.session_for("default.alice.default")
    s2 = await pool.session_for("default.alice.default")
    assert s1 is s2


async def test_read_pool_leases_independent_sessions_for_concurrent_calls(
    monkeypatch,
) -> None:
    started = 0
    release = asyncio.Event()

    async def call_tool(self, name, arguments):
        del self, name, arguments
        nonlocal started
        started += 1
        if started == 2:
            release.set()
        await asyncio.wait_for(release.wait(), timeout=0.2)
        return {"ok": True}

    monkeypatch.setattr(McpUserSession, "call_tool", call_tool)
    pool = McpReadSessionPool("http://a/mcp", size=2)

    first, second = await asyncio.gather(
        pool.call_tool("recall", {}),
        pool.call_tool("recall", {}),
    )

    assert first == second == {"ok": True}
    assert started == 2
    assert len(pool._sessions) == 2
    await pool.close()


async def test_read_pool_warmup_opens_every_bounded_transport(monkeypatch) -> None:
    warmed: set[McpUserSession] = set()

    async def tool_names(self):
        warmed.add(self)
        await asyncio.sleep(0)
        return frozenset({"eidolon_memory_recall_context"})

    monkeypatch.setattr(McpUserSession, "tool_names", tool_names)
    pool = McpReadSessionPool("http://a/mcp", size=3)

    await pool.warmup()

    assert len(warmed) == 3
    assert len(pool._sessions) == 3
    assert await pool.tool_names() == frozenset({"eidolon_memory_recall_context"})
    await pool.close()


async def test_client_pool_warms_every_reachable_realm(monkeypatch) -> None:
    warmed: list[str] = []

    async def warmup(self):
        warmed.append(self._url)

    monkeypatch.setattr(McpReadSessionPool, "warmup", warmup)
    pool = McpClientPool(
        routes=_routes(
            MemoryRoute(memory_space_id="realm-a", mcp_url="http://a/mcp"),
            MemoryRoute(memory_space_id="realm-b", mcp_url="http://b/mcp"),
            MemoryRoute(
                memory_space_id="realm-disabled",
                mcp_url="http://disabled/mcp",
                enabled=False,
            ),
        )
    )

    await pool.warmup_read_sessions()

    assert sorted(warmed) == ["http://a/mcp", "http://b/mcp"]
    await pool.close_all()


async def test_read_pool_closes_transport_in_its_owner_task_after_caller_timeout(
    monkeypatch,
) -> None:
    started = asyncio.Event()
    owner_tasks: dict[McpUserSession, asyncio.Task] = {}
    closed_tasks: dict[McpUserSession, asyncio.Task] = {}

    async def call_tool(self, name, arguments):
        del name, arguments
        owner_tasks[self] = asyncio.current_task()
        started.set()
        await asyncio.Event().wait()

    async def close(self):
        closed_tasks[self] = asyncio.current_task()

    monkeypatch.setattr(McpUserSession, "call_tool", call_tool)
    monkeypatch.setattr(McpUserSession, "close", close)
    pool = McpReadSessionPool("http://a/mcp", size=1)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(pool.call_tool("recall", {}), timeout=0.01)
    await started.wait()
    await pool.close()

    session = next(iter(owner_tasks))
    assert closed_tasks[session] is owner_tasks[session]


async def test_read_and_write_sessions_use_distinct_discovered_surfaces() -> None:
    pool = McpClientPool(
        routes=_routes(
            MemoryRoute(
                memory_space_id="default.alice.default",
                mcp_url="http://a/mcp",
                ops_mcp_url="http://a/ops/mcp",
            )
        )
    )

    read = await pool.session_for("default.alice.default")
    write = await pool.write_session_for("default.alice.default")

    assert read is not write
    assert read.matches(mcp_url="http://a/mcp", bearer_token=None)
    assert write.matches(mcp_url="http://a/ops/mcp", bearer_token=None)


async def test_session_rotated_when_url_changes() -> None:
    routes = _routes(MemoryRoute(memory_space_id="default.alice.default", mcp_url="http://old/mcp"))
    pool = McpClientPool(routes=routes)
    s1 = await pool.session_for("default.alice.default")
    # Swap the route under the table (simulating discovery refresh).
    s1.close = AsyncMock(return_value=None)
    routes._routes["default.alice.default"] = MemoryRoute(
        memory_space_id="default.alice.default",
        mcp_url="http://new/mcp",
    )
    s2 = await pool.session_for("default.alice.default")
    assert s2 is not s1
    s1.close.assert_awaited()  # old session was closed


async def test_pool_close_all_closes_each_session() -> None:
    pool = McpClientPool(
        routes=_routes(
            MemoryRoute(memory_space_id="default.alice.default", mcp_url="http://a/mcp"),
            MemoryRoute(memory_space_id="default.bob.default", mcp_url="http://b/mcp"),
        )
    )
    s1 = await pool.session_for("default.alice.default")
    s2 = await pool.session_for("default.bob.default")
    s1.close = AsyncMock()
    s2.close = AsyncMock()
    await pool.close_all()
    s1.close.assert_awaited_once()
    s2.close.assert_awaited_once()


async def test_pool_drop_session_closes_cached_session() -> None:
    pool = McpClientPool(
        routes=_routes(MemoryRoute(memory_space_id="default.alice.default", mcp_url="http://a/mcp"))
    )
    sess = await pool.session_for("default.alice.default")
    sess.close = AsyncMock()

    dropped = await pool.drop_session("default.alice.default", session=sess)

    assert dropped is True
    sess.close.assert_awaited_once()
    assert await pool.session_for("default.alice.default") is not sess


async def test_pool_drop_session_identity_guard_keeps_replacement() -> None:
    pool = McpClientPool(
        routes=_routes(MemoryRoute(memory_space_id="default.alice.default", mcp_url="http://a/mcp"))
    )
    stale = await pool.session_for("default.alice.default")
    replacement = McpUserSession("http://a/mcp")
    stale.close = AsyncMock()
    replacement.close = AsyncMock()
    pool._sessions["default.alice.default"] = replacement

    dropped = await pool.drop_session("default.alice.default", session=stale)

    assert dropped is False
    stale.close.assert_not_awaited()
    replacement.close.assert_not_awaited()
    assert await pool.session_for("default.alice.default") is replacement


# ---- McpUserSession ------------------------------------------------------


def _make_session(call_tool_return, *, raise_on_call: bool = False) -> McpUserSession:
    """Build a session with an injected fake MCP session so we don't import mcp."""
    sess = McpUserSession("http://x/mcp")
    fake = AsyncMock()
    if raise_on_call:
        fake.call_tool = AsyncMock(side_effect=RuntimeError("upstream broken"))
    else:
        fake.call_tool = AsyncMock(return_value=call_tool_return)
    sess._session = fake  # bypass the lazy MCP loader
    return sess


def _install_fake_mcp(monkeypatch, *, client_session_cls, client_cm):
    mcp_mod = ModuleType("mcp")
    client_mod = ModuleType("mcp.client")
    streamable_mod = ModuleType("mcp.client.streamable_http")
    session_mod = ModuleType("mcp.client.session")

    def streamable_http_client(url, headers=None):
        del url, headers
        return client_cm

    streamable_mod.streamable_http_client = streamable_http_client
    session_mod.ClientSession = client_session_cls
    client_mod.streamable_http = streamable_mod
    mcp_mod.client = client_mod
    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.client", client_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", streamable_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.session", session_mod)


async def test_call_tool_returns_dict_on_dict_decode() -> None:
    raw = SimpleNamespace(
        isError=False,
        structuredContent={"records": [{"id": "r1", "value": "v"}]},
        content=[],
    )
    sess = _make_session(raw)
    out = await sess.call_tool("eidolon_memory_recall_context", {"query": "x"})
    assert out == {"records": [{"id": "r1", "value": "v"}]}


async def test_call_tool_wraps_list_decode_in_records() -> None:
    raw = SimpleNamespace(isError=False, structuredContent=[1, 2, 3], content=[])
    sess = _make_session(raw)
    out = await sess.call_tool("any", {})
    assert out == {"records": [1, 2, 3]}


async def test_call_tool_wraps_scalar_decode_in_result_key() -> None:
    raw = SimpleNamespace(isError=False, structuredContent=42, content=[])
    sess = _make_session(raw)
    out = await sess.call_tool("any", {})
    assert out == {"result": 42}


async def test_tool_names_cleans_half_open_session_when_initialize_is_cancelled(
    monkeypatch,
) -> None:
    class FakeClientContext:
        exited = False

        async def __aenter__(self):
            return object(), object(), None

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True

    sessions = []

    class FakeClientSession:
        def __init__(self, read, write):
            del read, write
            self.exited = False
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True

        async def initialize(self):
            raise asyncio.CancelledError("probe cancelled by mcp transport")

    client_cm = FakeClientContext()
    _install_fake_mcp(
        monkeypatch,
        client_session_cls=FakeClientSession,
        client_cm=client_cm,
    )
    sess = McpUserSession("http://x/mcp")

    assert await sess.tool_names() is None
    assert client_cm.exited is True
    assert sessions and sessions[0].exited is True
    assert sess._session is None
    assert sess._client_cm is None
    assert sess._http_client is None


async def test_call_tool_wraps_connect_failure_and_cleans_partial_session(monkeypatch) -> None:
    class FakeClientContext:
        exited = False

        async def __aenter__(self):
            return object(), object(), None

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True

    sessions = []

    class FakeClientSession:
        def __init__(self, read, write):
            del read, write
            self.exited = False
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.exited = True

        async def initialize(self):
            raise RuntimeError("connect failed")

    client_cm = FakeClientContext()
    _install_fake_mcp(
        monkeypatch,
        client_session_cls=FakeClientSession,
        client_cm=client_cm,
    )
    sess = McpUserSession("http://x/mcp")

    with pytest.raises(MemoryUnavailableError, match="connect failed"):
        await sess.call_tool("any", {})
    assert client_cm.exited is True
    assert sessions and sessions[0].exited is True
    assert sess._session is None
    assert sess._client_cm is None
    assert sess._http_client is None


# ---- capability negotiation ----------------------------------------------


async def test_supports_true_when_server_advertises_tool() -> None:
    sess = _make_session({})
    sess._session.list_tools = AsyncMock(
        return_value=SimpleNamespace(
            tools=[
                SimpleNamespace(name="eidolon_memory_recall_context"),
                SimpleNamespace(name="eidolon_memory_active_commitments"),
            ]
        )
    )
    assert await sess.supports("eidolon_memory_active_commitments") is True
    assert await sess.tool_names() == frozenset(
        {"eidolon_memory_recall_context", "eidolon_memory_active_commitments"}
    )


async def test_supports_false_when_tool_absent() -> None:
    sess = _make_session({})
    sess._session.list_tools = AsyncMock(
        return_value=SimpleNamespace(tools=[SimpleNamespace(name="eidolon_memory_recall_context")])
    )
    assert await sess.supports("eidolon_memory_active_commitments") is False


async def test_supports_optimistic_when_probe_fails() -> None:
    # list_tools failing must NOT block calls — unknown means "attempt anyway".
    sess = _make_session({})
    sess._session.list_tools = AsyncMock(side_effect=RuntimeError("probe failed"))
    assert await sess.tool_names() is None
    assert await sess.supports("eidolon_memory_active_commitments") is True


async def test_tool_names_cached_after_first_probe() -> None:
    sess = _make_session({})
    probe = AsyncMock(return_value=SimpleNamespace(tools=[SimpleNamespace(name="a")]))
    sess._session.list_tools = probe
    await sess.supports("a")
    await sess.supports("a")
    probe.assert_awaited_once()  # cached, probed once


async def test_call_tool_raises_memory_unavailable_on_exception() -> None:
    sess = _make_session(None, raise_on_call=True)
    with pytest.raises(MemoryUnavailableError, match="upstream broken"):
        await sess.call_tool("any", {})


async def test_session_close_is_idempotent_and_drops_refs() -> None:
    sess = McpUserSession("http://x/mcp")
    sess._session = AsyncMock()
    sess._session.__aexit__ = AsyncMock()
    sess._client_cm = AsyncMock()
    sess._client_cm.__aexit__ = AsyncMock()
    sess._http_client = AsyncMock()
    await sess.close()
    assert sess._session is None
    assert sess._client_cm is None
    assert sess._http_client is None
    # Second close is a no-op (no AttributeError).
    await sess.close()


def test_session_matches_url_and_token() -> None:
    sess = McpUserSession("http://x/mcp", bearer_token="tok")
    assert sess.matches(mcp_url="http://x/mcp", bearer_token="tok")
    assert not sess.matches(mcp_url="http://y/mcp", bearer_token="tok")
    assert not sess.matches(mcp_url="http://x/mcp", bearer_token="other")


# ---- transient_mcp_session -----------------------------------------------


async def test_transient_session_closes_on_exit() -> None:
    """The context manager yields a fresh session and closes it after the
    ``async with`` block, even when the block raises."""
    async with transient_mcp_session("http://x/mcp") as sess:
        assert isinstance(sess, McpUserSession)
        sess.close = AsyncMock()  # swap before exit so we can observe
    sess.close.assert_awaited_once()


async def test_failed_discovery_probe_does_not_close_a_working_read_pool(monkeypatch) -> None:
    from eidolon_agent.infra.memory.discovery import DiscoveryResponse

    route = MemoryRoute(memory_space_id="default.alice.default", mcp_url="http://a/mcp")
    routes = _routes(route)
    pool = McpClientPool(routes=routes)
    session = await pool.session_for(route.memory_space_id)

    async def call_tool(self, name, arguments):
        return {"records": [{"memory_id": "remembered"}]}

    monkeypatch.setattr(McpUserSession, "call_tool", call_tool)
    try:
        for reachable in (False, True):
            await routes.replace_from_discovery(
                DiscoveryResponse.model_validate(
                    {
                        "nats": {"url": "nats://x"},
                        "memory_realms": [
                            {
                                "memory_space_id": route.memory_space_id,
                                "mcp_http_url": route.mcp_url,
                                "ops_mcp_http_url": route.mcp_url,
                                "agent_reachable": reachable,
                            }
                        ],
                    }
                )
            )
            assert await pool.health() is reachable
            current = await pool.session_for(route.memory_space_id)
            assert current is session
            assert await asyncio.wait_for(current.call_tool("recall", {}), 0.5) == {
                "records": [{"memory_id": "remembered"}]
            }
    finally:
        await pool.close_all()
