"""Per-companion tool visibility policy.

The static tool registry is global (built once at boot), but which of those
tools a given companion may see/call is per-companion — driven by
``companions.runtime_config_json`` (allow/deny). This pure filter is applied per
turn to the schema list the LLM sees. Dispatch-side deny enforcement lives in
the dispatcher (via ``ToolInvocationContext.denied_tools``) so a hallucinated
denied name can never actuate even though it is a real registered tool.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from eidolon_agent.core.types.tool import ToolSchema


class ToolVisibilityPolicy:
    @staticmethod
    def filter(
        schemas: Sequence[ToolSchema],
        *,
        allow: Iterable[str] = (),
        deny: Iterable[str] = (),
    ) -> list[ToolSchema]:
        """Return the schemas visible to this companion.

        - ``deny`` always removes a tool.
        - a non-empty ``allow`` turns the set into an allowlist (only listed
          tools survive); an empty ``allow`` means "all but denied".
        """
        allow_set = frozenset(allow)
        deny_set = frozenset(deny)
        out: list[ToolSchema] = []
        for schema in schemas:
            if schema.name in deny_set:
                continue
            if allow_set and schema.name not in allow_set:
                continue
            out.append(schema)
        return out
