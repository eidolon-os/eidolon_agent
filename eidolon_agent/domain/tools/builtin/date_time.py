"""Time/date context tool."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.tool import ToolCall, ToolResult, ToolSchema

_LOCALE_TIMEZONES = {
    "zh-CN": "Asia/Shanghai",
    "zh-HK": "Asia/Hong_Kong",
    "zh-TW": "Asia/Taipei",
    "ja-JP": "Asia/Tokyo",
    "ko-KR": "Asia/Seoul",
    "en-US": "America/Los_Angeles",
}

_WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


class GetTimeTool:
    schema = ToolSchema(
        name="get_time",
        description=(
            "Get the current local date/time context. Use when the user asks about "
            "the current time, date, weekday, timezone, or relative day context."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": (
                        "Optional IANA timezone, for example Asia/Shanghai or "
                        "America/Los_Angeles. Defaults from the caller locale."
                    ),
                }
            },
            "required": [],
            "additionalProperties": False,
        },
        timeout_s=0.05,
    )

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        timezone = str(call.arguments.get("timezone") or "").strip()
        if not timezone:
            timezone = _LOCALE_TIMEZONES.get(ctx.turn_context.locale, "UTC")
        try:
            tz = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="invalid_timezone",
                error_message=f"unknown timezone: {timezone}",
            )

        now = datetime.now(tz)
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={
                "iso_datetime": now.isoformat(timespec="seconds"),
                "date": now.date().isoformat(),
                "time": now.strftime("%H:%M:%S"),
                "weekday": now.strftime("%A"),
                "weekday_zh": _WEEKDAYS_ZH[now.weekday()],
                "timezone": timezone,
                "utc_offset": now.strftime("%z"),
                "locale": ctx.turn_context.locale,
            },
        )
