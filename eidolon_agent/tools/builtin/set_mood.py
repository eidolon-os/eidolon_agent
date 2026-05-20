"""``set_mood`` — adjust MindState mood. Side-effectful (mutates internal state)."""

from __future__ import annotations

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema


class SetMoodTool:
    schema = ToolSchema(
        name="set_mood",
        description="Adjust the assistant's mood. Used sparingly — usually mood updates from event reflectors.",
        json_schema={
            "type": "object",
            "properties": {
                "emotion": {
                    "type": "string",
                    "enum": ["joy", "sad", "anger", "fear", "surprise"],
                },
                "delta": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                "reason": {"type": "string"},
            },
            "required": ["emotion", "delta"],
            "additionalProperties": False,
        },
        permissions=frozenset({Permission.SYSTEM}),
        side_effect=True,
        timeout_s=0.1,
    )

    def __init__(self, mind_service=None) -> None:
        # Wired by bootstrap; nullable lets tests stub easily.
        self._mind = mind_service

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._mind is None:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="mind_unavailable",
                error_message="MindService not wired into this agent instance",
            )
        emotion = call.arguments["emotion"]
        delta = float(call.arguments["delta"])
        self._mind.apply_event_sync(instance_id=ctx.caller.agent_instance_id or "", emotion=emotion, delta=delta)
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={"emotion": emotion, "delta": delta},
        )
