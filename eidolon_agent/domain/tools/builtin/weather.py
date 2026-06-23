"""Weather lookup tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema

WeatherFetcher = Callable[[str, str], Awaitable[dict[str, Any]]]


class GetWeatherTool:
    def __init__(
        self,
        *,
        fetcher: WeatherFetcher | None = None,
        default_location: str = "Shanghai",
        timeout_s: float = 2.0,
    ) -> None:
        self.schema = ToolSchema(
            name="get_weather",
            description=(
                "Get current weather and a short forecast for a location. Use when the "
                "user asks about weather, temperature, rain, wind, or forecast. If the "
                "user does not name a location, use the configured default location."
            ),
            json_schema={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City or place name, for example 上海, 杭州, Tokyo, or Paris.",
                    },
                    "lang": {
                        "type": "string",
                        "description": "Language code for place lookup. Defaults from caller locale.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            permissions=frozenset({Permission.NETWORK}),
            timeout_s=timeout_s,
        )
        self._fetcher = fetcher or _fetch_open_meteo
        self._default_location = default_location

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        location = str(call.arguments.get("location") or "").strip() or self._default_location
        lang = str(call.arguments.get("lang") or "").strip() or _weather_lang(ctx.caller.locale)
        try:
            data = await self._fetcher(location, lang)
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="weather_lookup_failed",
                error_message=str(exc),
            )
        return ToolResult(call_id=call.id, name=self.schema.name, ok=True, content=data)


async def _fetch_open_meteo(location: str, lang: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=1.5, follow_redirects=True) as client:
        geo_resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": location,
                "count": 1,
                "language": lang,
                "format": "json",
            },
        )
        geo_resp.raise_for_status()
        geo = geo_resp.json()
        matches = geo.get("results") or []
        if not matches:
            raise ValueError(f"location not found: {location}")
        place = matches[0]
        forecast_resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": "temperature_2m,relative_humidity_2m,precipitation,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 3,
            },
        )
        forecast_resp.raise_for_status()
        forecast = forecast_resp.json()

    current = forecast.get("current") or {}
    units = forecast.get("current_units") or {}
    daily = forecast.get("daily") or {}
    return {
        "location": {
            "name": place.get("name") or location,
            "country": place.get("country"),
            "admin1": place.get("admin1"),
            "latitude": place.get("latitude"),
            "longitude": place.get("longitude"),
            "timezone": forecast.get("timezone"),
        },
        "current": {
            "temperature": current.get("temperature_2m"),
            "temperature_unit": units.get("temperature_2m"),
            "humidity": current.get("relative_humidity_2m"),
            "humidity_unit": units.get("relative_humidity_2m"),
            "precipitation": current.get("precipitation"),
            "precipitation_unit": units.get("precipitation"),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_speed_unit": units.get("wind_speed_10m"),
            "time": current.get("time"),
        },
        "forecast_days": [
            {
                "date": date,
                "temperature_max": _at(daily.get("temperature_2m_max"), idx),
                "temperature_min": _at(daily.get("temperature_2m_min"), idx),
                "precipitation_probability_max": _at(
                    daily.get("precipitation_probability_max"), idx
                ),
            }
            for idx, date in enumerate(daily.get("time") or [])
        ],
        "source": "open-meteo",
    }


def _at(values: object, idx: int) -> object:
    if not isinstance(values, list) or idx >= len(values):
        return None
    return values[idx]


def _weather_lang(locale: str) -> str:
    normalized = (locale or "").split("-")[0].lower()
    return normalized or "en"
