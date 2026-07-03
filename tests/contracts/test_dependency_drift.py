"""Static symbol-drift guard for the eidolon_sdk / eidolon_data seams.

eidolon_agent depends on eidolon_sdk and eidolon_data by import. When a
neighbor renames or removes a symbol, the break is silent until the exact
code path runs (that is how the memory-recall tuple-unpack regression slipped
through). This test parses every agent source file, collects every
``from eidolon_sdk...``/``from eidolon_data...`` import, and asserts each
imported name still resolves — failing in CI with the full list of broken
symbols instead of at runtime.

Pure static analysis via AST: it imports the *target* modules (sdk/data,
side-effect-free) but never executes agent modules.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_WATCHED = ("eidolon_sdk", "eidolon_data")
_AGENT_PKG = Path(__file__).resolve().parents[2] / "eidolon_agent"


def _agent_source_files() -> list[Path]:
    return [
        p
        for p in _AGENT_PKG.rglob("*.py")
        if "__pycache__" not in p.parts
        # Generated protobuf stubs carry their own import quirks.
        and not p.name.endswith("_pb2.py")
        and not p.name.endswith("_pb2_grpc.py")
    ]


def _watched(module: str | None) -> bool:
    return bool(module) and module.split(".", 1)[0] in _WATCHED


def _collect_imports() -> list[tuple[str, str, Path]]:
    """Return (module, symbol_name, source_file) for every watched import."""
    out: list[tuple[str, str, Path]] = []
    for path in _agent_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and _watched(node.module):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    out.append((node.module, alias.name, path))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _watched(alias.name):
                        out.append((alias.name, "", path))
    return out


def _resolves(module: str, name: str) -> bool:
    try:
        mod = importlib.import_module(module)
    except Exception:
        return False
    if not name:  # plain `import eidolon_sdk.x`
        return True
    if hasattr(mod, name):
        return True
    # The name may be a submodule rather than an attribute.
    try:
        importlib.import_module(f"{module}.{name}")
        return True
    except Exception:
        return False


def test_no_sdk_or_data_symbol_drift() -> None:
    broken = [
        f"{module}.{name or '<module>'}  (imported in {path.relative_to(_AGENT_PKG.parent)})"
        for module, name, path in _collect_imports()
        if not _resolves(module, name)
    ]
    assert not broken, "eidolon_sdk/eidolon_data symbols agent imports no longer resolve:\n" + "\n".join(
        sorted(broken)
    )


def test_drift_check_actually_scans_imports() -> None:
    # Guard the guard: if the collector finds nothing, the test above is a
    # no-op and would hide real drift.
    collected = _collect_imports()
    assert len(collected) > 20, f"expected many watched imports, found {len(collected)}"
