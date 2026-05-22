"""ContextCompiler + Providers.

The compiler runs every registered :class:`ContextProvider` concurrently with
a soft per-provider deadline, sorts the resulting :class:`ContextSegment`s by
weight, prunes from the bottom until they fit the LLM's token budget, and
assembles the final message list.
"""

from eidolon_agent.domain.context.compiler import ContextCompiler

__all__ = ["ContextCompiler"]
