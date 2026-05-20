"""``emit_event`` — publish a custom event to the EventBus."""

from __future__ import annotations

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema


class EmitEventTool:
    schema = ToolSchema(
        name="emit_event",
        description="Publish an event to the internal bus (e.g. to notify a tool / schedule a follow-up).",
        json_schema={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "payload": {"type": "object"},
            },
            "required": ["subject"],
            "additionalProperties": False,
        },
        permissions=frozenset({Permission.SYSTEM}),
        side_effect=True,
        timeout_s=0.2,
    )

    def __init__(self, event_bus=None) -> None:
        self._bus = event_bus

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._bus is None:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="event_bus_unavailable",
                error_message="EventBus not wired",
            )
        subject = call.arguments["subject"]
        payload = call.arguments.get("payload") or {}
        await self._bus.publish(
            Event(subject=subject, payload=payload, trace_id=ctx.caller.trace_id, source="tool.emit_event")
        )
        return ToolResult(
            call_id=call.id, name=self.schema.name, ok=True, content={"subject": subject}
        )
