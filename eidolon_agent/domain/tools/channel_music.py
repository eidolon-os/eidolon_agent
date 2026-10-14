from __future__ import annotations

from typing import Any

from eidolon_agent.core.ports.channel_tool import ChannelToolExecutor, ChannelToolRequest
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema


class ChannelMusicTool:
    def __init__(
        self,
        executor: ChannelToolExecutor,
        *,
        name: str,
        description: str,
        json_schema: dict[str, Any],
        side_effect: bool,
        timeout_s: float,
    ) -> None:
        self._executor = executor
        self.schema = ToolSchema(
            name=name,
            description=description,
            json_schema=json_schema,
            permissions=frozenset({Permission.SYSTEM}),
            timeout_s=timeout_s,
            side_effect=side_effect,
            idempotency_key_template="${turn_id}:${arguments_json}" if side_effect else None,
        )

    async def invoke(self, call: ToolCall, *, ctx) -> ToolResult:
        return await self._executor.execute(
            ChannelToolRequest(
                turn_id=ctx.turn_id,
                call_id=call.id,
                name=self.schema.name,
                arguments=call.arguments,
            )
        )


def build_channel_music_tools(executor: ChannelToolExecutor) -> list[ChannelMusicTool]:
    return [
        ChannelMusicTool(
            executor,
            name="music.play",
            description="Play an authorized online music song by its provider song id.",
            json_schema={
                "type": "object",
                "properties": {
                    "song_id": {"type": "string", "minLength": 1},
                    "playback_ref": {"type": "string", "minLength": 1},
                },
                "required": ["song_id"],
                "additionalProperties": False,
            },
            side_effect=True,
            timeout_s=60.0,
        ),
        ChannelMusicTool(
            executor,
            name="music.stop",
            description="Stop the current online music playback.",
            json_schema={
                "type": "object",
                "properties": {"playback_ref": {"type": "string", "minLength": 1}},
                "additionalProperties": False,
            },
            side_effect=True,
            timeout_s=10.0,
        ),
        ChannelMusicTool(
            executor,
            name="music.status",
            description="Read the current online music playback status.",
            json_schema={"type": "object", "properties": {}, "additionalProperties": False},
            side_effect=False,
            timeout_s=5.0,
        ),
    ]
