"""Architecture sanity tests.

These cover invariants that import-linter alone doesn't catch — chiefly,
that cross-LAYER imports respect the public API of the target module
(go through the top-level package, not directly into internal files).

Layer rules (enforced strictly by import-linter, see pyproject.toml):
    core ← domain ← infra
    everything ← app

The strict contracts are checked by `lint-imports` in CI. This module adds
softer sanity checks that complement them.
"""

from __future__ import annotations

import ast
from pathlib import Path

LAYERS = ("domain", "infra", "app")
ROOT = Path(__file__).resolve().parent.parent / "eidolon_agent"

# The wiring layer (DI / bootstrap) is the one place where deep imports are
# legitimate by design — it must know about concrete classes to construct them.
_WIRING_EXEMPT = frozenset({"eidolon_agent.app.runtime.bootstrap"})


def _top_modules() -> dict[str, Path]:
    """Map ``eidolon_agent.<layer>.<module>`` → directory."""
    out: dict[str, Path] = {}
    for layer in LAYERS:
        for d in (ROOT / layer).iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                out[f"eidolon_agent.{layer}.{d.name}"] = d
    return out


def test_no_cross_layer_deep_imports() -> None:
    """An import crossing layer boundaries must terminate at a module's
    package (``eidolon_agent.<layer>.<module>``) or its dedicated types
    sub-module — not at an internal file.

    Within the same layer, deeper imports are permitted (e.g.
    ``domain.agent.turn`` may use ``domain.context.compiler.ContextCompiler``).
    The strict layer separation is left to import-linter.
    """
    top = _top_modules()
    leaks: list[tuple[str, str, int]] = []
    for py in ROOT.rglob("*.py"):
        if "__pycache__" in str(py) or "_pb2" in py.name:
            continue
        src_mod = ".".join(py.with_suffix("").relative_to(ROOT.parent).parts)
        if src_mod in _WIRING_EXEMPT:
            continue
        src_layer = src_mod.split(".", 2)[1] if "." in src_mod[len("eidolon_agent."):] else ""
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            for pkg in top:
                if not node.module.startswith(pkg + "."):
                    continue
                dst_layer = pkg.split(".")[1]
                if dst_layer == src_layer:
                    break  # same layer, deep is OK
                # cross-layer — only allow .types as terminal
                tail = node.module[len(pkg) + 1:]
                if tail in ("types", "ports"):
                    break
                leaks.append((src_mod, node.module, node.lineno))
                break
    assert not leaks, "cross-layer deep imports (must go through pkg __init__.py):\n" + "\n".join(
        f"  {s}:{ln} -> {d}" for s, d, ln in leaks
    )
