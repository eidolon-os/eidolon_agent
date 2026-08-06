"""Module-local fixtures for ``domain/tools`` tests."""

from __future__ import annotations

import pytest

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.tool import ToolCall, ToolSchema
from eidolon_agent.core.types.turn_context import TurnContext


@pytest.fixture
def tool_ctx() -> ToolInvocationContext:
    return ToolInvocationContext(
        turn_context=TurnContext(
            owner_id="owner-1",
            companion_id="companion-1",
            device_id="device-1",
            memory_realm_id="realm-1",
            genome_id="genome-1",
            trace_id="trace",
            request_id="req",
        ),
        input_modality="text",
        turn_id="turn-1",
        session_id="rs-test",
    )


class _StubTool:
    """Minimal tool used in registry/dispatcher tests. Configurable per case."""

    def __init__(
        self,
        name: str,
        *,
        side_effect: bool = False,
        invoke=None,
        timeout_s: float = 1.0,
        json_schema: dict | None = None,
        permissions=frozenset(),
        idempotency_key_template: str | None = None,
    ) -> None:
        self.schema = ToolSchema(
            name=name,
            description=f"stub {name}",
            json_schema=json_schema or {"type": "object", "additionalProperties": True},
            permissions=frozenset(permissions),
            side_effect=side_effect,
            timeout_s=timeout_s,
            idempotency_key_template=idempotency_key_template,
        )
        self._invoke = invoke
        self.calls: list[ToolCall] = []

    async def invoke(self, call, *, ctx):
        self.calls.append(call)
        if self._invoke is not None:
            return await self._invoke(call, ctx)
        from eidolon_agent.core.types.tool import ToolResult

        return ToolResult(call_id=call.id, name=self.schema.name, ok=True, content={})


@pytest.fixture
def stub_tool_factory():
    return _StubTool
