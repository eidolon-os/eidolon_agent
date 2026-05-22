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
    _decode_call_tool_result,
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
