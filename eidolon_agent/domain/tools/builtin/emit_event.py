"""``emit_event`` — publish a custom event to the EventBus."""

from __future__ import annotations

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema


class EmitEventTool:
    schema = ToolSchema(
        name="emit_event",
        description=(
            "Publish a side-effectful event to the internal agent bus. Use only when an "
            "internal system action, notification, or follow-up event must be emitted. "
            "Requires a concrete subject and optional JSON payload; do not use for ordinary "
            "conversation or external long-running work."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Internal event subject to publish, for example 'agent.test.example'.",
                },
                "payload": {
                    "type": "object",
                    "description": "JSON object payload for subscribers. Defaults to an empty object.",
                },
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
            Event(
                subject=subject,
                payload=payload,
                trace_id=ctx.turn_context.trace_id,
                source="tool.emit_event",
            )
        )
        return ToolResult(
            call_id=call.id, name=self.schema.name, ok=True, content={"subject": subject}
        )
