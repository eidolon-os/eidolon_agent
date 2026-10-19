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
        llm_name: str,
        description: str,
        json_schema: dict[str, Any],
        side_effect: bool,
        timeout_s: float,
    ) -> None:
        self._executor = executor
        self.schema = ToolSchema(
            name=name,
            llm_name=llm_name,
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
            name="music.search",
            llm_name="music_search",
            description=(
                "Search authorized online music. Provide title and artist separately when known; "
                "the capability performs bounded query fallback. Results are non-exhaustive; "
                "unverified candidates are not confirmed matches. Return candidate provider song ids. "
                "Use the selected song_id with music_play without repeating search. "
                "Do not substitute a different song without user agreement. "
                "Use keyword 'random' for recommendations."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "artist": {"type": "string", "minLength": 1},
                    "version": {"type": "string"},
                    "language": {"type": "string"},
                },
                "anyOf": [{"required": [key]} for key in ("keyword", "title", "artist")],
                "additionalProperties": False,
            },
            side_effect=False,
            timeout_s=15.0,
        ),
        ChannelMusicTool(
            executor,
            name="music.play",
            llm_name="music_play",
            description=("Start a new playback of a selected authorized song by its provider song id, "
                "including replaying a stopped or completed song from the beginning. "
                "Acceptance does not mean audio has started. Use music_status for playback state; "
                "do not claim audible playback from acceptance alone."),
            json_schema={
                "type": "object",
                "properties": {
                    "song_id": {"type": "string", "minLength": 1},
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
            llm_name="music_stop",
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
            llm_name="music_status",
            description="Read the current online music playback status.",
            json_schema={"type": "object", "properties": {}, "additionalProperties": False},
            side_effect=False,
            timeout_s=5.0,
        ),
    ]
