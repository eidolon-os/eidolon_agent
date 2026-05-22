"""ContextCompiler — fixed-shape prompt assembly for one Turn.

No pluggable providers; the hot path is intentionally direct.
"""

from eidolon_agent.domain.context.compiler import ContextCompiler

__all__ = ["ContextCompiler"]
