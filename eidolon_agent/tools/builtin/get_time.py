"""``get_time`` — return the current wall-clock time for the user's locale.

Trivial, but exemplifies the minimum boilerplate to implement a tool.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.tool import ToolCall, ToolResult, ToolSchema


class GetTimeTool:
    schema = ToolSchema(
        name="get_time",
        description="Return the current local time. Optionally accepts a timezone name (IANA, e.g. 'Asia/Shanghai').",
        json_schema={
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "IANA tz name; defaults to caller locale."},
                "format": {"type": "string", "description": "strftime fmt; default '%Y-%m-%d %H:%M:%S %Z'."},
            },
            "additionalProperties": False,
        },
        side_effect=False,
        timeout_s=0.2,
    )

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        tz_name = call.arguments.get("timezone") or _locale_to_tz(ctx.caller.locale)
        fmt = call.arguments.get("format") or "%Y-%m-%d %H:%M:%S %Z"
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="invalid_timezone",
                error_message=f"unknown timezone: {tz_name}",
            )
        now = datetime.now(tz)
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={"now": now.strftime(fmt), "iso": now.isoformat(), "tz": tz_name},
        )


def _locale_to_tz(locale: str) -> str:
    return {
        "zh-CN": "Asia/Shanghai",
        "ja-JP": "Asia/Tokyo",
        "en-US": "America/Los_Angeles",
    }.get(locale, "UTC")
