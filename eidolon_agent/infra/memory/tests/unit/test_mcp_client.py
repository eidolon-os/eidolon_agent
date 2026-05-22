"""McpUserSession + McpClientPool + result decoder — MCP SDK mocked.

We don't hit any real MCP server. Tests cover:
- ``_decode_call_tool_result`` for structured, JSON-text, and error blocks
- ``McpClientPool.session_for`` reuses sessions when routes are stable and
  rotates them when the route changes (URL or token)
- ``health()`` reflects route availability
"""

from __future__ import annotations

from types import SimpleNamespace
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
        nats=MemoryNatsRoute(url="nats://x", stream="", turn_subject_template="t", cmd_subject_template="c"),
        routes={r.user_id: r for r in routes},
    )


async def test_pool_health_true_when_routes_present() -> None:
    pool = McpClientPool(routes=_routes(MemoryRoute(user_id="alice", mcp_url="http://a/mcp")))
    assert await pool.health() is True


async def test_pool_health_false_when_no_routes() -> None:
    pool = McpClientPool(routes=_routes())
    assert await pool.health() is False


async def test_session_for_unknown_user_raises_unavailable() -> None:
    pool = McpClientPool(routes=_routes())
    with pytest.raises(MemoryUnavailableError, match="no reachable MCP endpoint"):
        await pool.session_for("ghost")


async def test_session_reused_for_same_route() -> None:
    pool = McpClientPool(
        routes=_routes(MemoryRoute(user_id="alice", mcp_url="http://a/mcp", bearer_token="t1"))
    )
    s1 = await pool.session_for("alice")
    s2 = await pool.session_for("alice")
    assert s1 is s2


async def test_session_rotated_when_url_changes() -> None:
    routes = _routes(MemoryRoute(user_id="alice", mcp_url="http://old/mcp"))
    pool = McpClientPool(routes=routes)
    s1 = await pool.session_for("alice")
    # Swap the route under the table (simulating discovery refresh).
    s1.close = AsyncMock(return_value=None)
    routes._routes["alice"] = MemoryRoute(user_id="alice", mcp_url="http://new/mcp")
    s2 = await pool.session_for("alice")
    assert s2 is not s1
    s1.close.assert_awaited()  # old session was closed


async def test_pool_close_all_closes_each_session() -> None:
    pool = McpClientPool(
        routes=_routes(
            MemoryRoute(user_id="alice", mcp_url="http://a/mcp"),
            MemoryRoute(user_id="bob", mcp_url="http://b/mcp"),
        )
    )
    s1 = await pool.session_for("alice")
    s2 = await pool.session_for("bob")
    s1.close = AsyncMock()
    s2.close = AsyncMock()
    await pool.close_all()
    s1.close.assert_awaited_once()
    s2.close.assert_awaited_once()


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


async def test_call_tool_returns_dict_on_dict_decode() -> None:
    raw = SimpleNamespace(
        isError=False,
        structuredContent={"records": [{"id": "r1", "value": "v"}]},
        content=[],
    )
    sess = _make_session(raw)
    out = await sess.call_tool("eidolon_memory_search", {"q": "x"})
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
