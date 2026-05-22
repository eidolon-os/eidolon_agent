"""ToolRegistry — register / lookup / list / dedup."""

from __future__ import annotations

import pytest

from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.tools import ToolRegistry

pytestmark = pytest.mark.unit


def test_register_and_get(stub_tool_factory) -> None:
    reg = ToolRegistry()
    tool = stub_tool_factory("alpha")
    reg.register(tool)
    assert reg.get("alpha") is tool


def test_register_duplicate_raises(stub_tool_factory) -> None:
    reg = ToolRegistry()
    reg.register(stub_tool_factory("alpha"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(stub_tool_factory("alpha"))


def test_get_missing_raises_not_found() -> None:
    reg = ToolRegistry()
    with pytest.raises(NotFoundError):
        reg.get("nope")


def test_deregister(stub_tool_factory) -> None:
    reg = ToolRegistry()
    reg.register(stub_tool_factory("alpha"))
    reg.deregister("alpha")
    with pytest.raises(NotFoundError):
        reg.get("alpha")


def test_deregister_missing_is_noop() -> None:
    ToolRegistry().deregister("ghost")  # no error


def test_list_schemas_returns_all(stub_tool_factory) -> None:
    reg = ToolRegistry()
    reg.register(stub_tool_factory("a"))
    reg.register(stub_tool_factory("b"))
    names = {s.name for s in reg.list_schemas()}
    assert names == {"a", "b"}
