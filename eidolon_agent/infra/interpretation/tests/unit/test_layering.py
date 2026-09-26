"""Interpretation adapters stay extractable into their own service.

An adapter may import only the stdlib, pydantic/httpx, the SDK's interpretation
and smart-home contracts, and its own package (the shared lexicon) — never the
Agent's directory, tools, sessions or anything else in ``eidolon_agent``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ADAPTERS = Path(__file__).resolve().parents[2] / "adapters"
OWN_PACKAGE = "eidolon_agent.infra.interpretation.adapters"
ALLOWED = (
    "pydantic",
    "httpx",
    "eidolon_sdk.biz.interpretation",
    "eidolon_sdk.biz.smarthome",
    OWN_PACKAGE,
)


def _imports(path: Path) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: only inside this package
                found.append((f"{OWN_PACKAGE}.{node.module or ''}".rstrip("."), node.lineno))
            elif node.module:
                found.append((node.module, node.lineno))
    return found


def _allowed(module: str) -> bool:
    top = module.split(".")[0]
    if top == "__future__" or top in sys.stdlib_module_names:
        return True
    return any(module == prefix or module.startswith(prefix + ".") for prefix in ALLOWED)


def test_adapters_import_only_contracts_and_their_own_helpers() -> None:
    files = sorted(p for p in ADAPTERS.rglob("*.py") if "tests" not in p.parts)
    assert {p.name for p in files} >= {"rules.py", "laya.py", "lexicon.py"}
    leaks = [
        f"{path.name}:{line} imports {module}"
        for path in files
        for module, line in _imports(path)
        if not _allowed(module)
    ]
    assert leaks == []


@pytest.mark.parametrize(
    "module",
    [
        "eidolon_agent.domain.smarthome",
        "eidolon_agent.core.ports.tool",
        "eidolon_agent.infra.interpretation.recording",
        "eidolon_sdk.biz.body",
        "loguru",
    ],
)
def test_the_rule_rejects_agent_internals(module: str) -> None:
    assert not _allowed(module)
