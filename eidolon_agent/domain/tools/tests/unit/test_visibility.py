"""P3: per-companion tool visibility policy (allow/deny)."""

from __future__ import annotations

from eidolon_agent.core.types.tool import ToolSchema
from eidolon_agent.domain.tools.visibility import ToolVisibilityPolicy


def _s(name: str) -> ToolSchema:
    return ToolSchema(name=name, description=name, json_schema={"type": "object"})


def test_deny_removes_named_tools():
    out = ToolVisibilityPolicy.filter([_s("a"), _s("b")], deny=["b"])
    assert [s.name for s in out] == ["a"]


def test_empty_allow_keeps_all_but_denied():
    out = ToolVisibilityPolicy.filter([_s("a"), _s("b"), _s("c")], deny=["c"])
    assert {s.name for s in out} == {"a", "b"}


def test_nonempty_allow_is_an_allowlist():
    out = ToolVisibilityPolicy.filter([_s("a"), _s("b"), _s("c")], allow=["a", "c"])
    assert {s.name for s in out} == {"a", "c"}


def test_deny_wins_over_allow():
    out = ToolVisibilityPolicy.filter([_s("a"), _s("b")], allow=["a", "b"], deny=["b"])
    assert {s.name for s in out} == {"a"}
