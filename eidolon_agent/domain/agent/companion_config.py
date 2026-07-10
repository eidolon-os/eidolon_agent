"""Per-companion runtime configuration (``companions.runtime_config_json``).

The single agent codebase is differentiated per companion by three layers:
its persona genome (personality/prompt), its memory realm, and its *operational*
config — model routing, tool allow/deny, and policy toggles. That last layer
lives in ``companions.runtime_config_json`` (sovereign data, admin-editable) and
is resolved here, per turn, off a short-TTL cache so admin edits take effect
without evicting the long-lived per-companion ``TurnEngine``.

Parsing is defensive: unknown keys are ignored and malformed values fall back to
safe defaults, so operators can add switches without breaking the hot path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

_DEFAULT_TEMPERATURE = 0.7


@dataclass(frozen=True, slots=True)
class CompanionRuntimeConfig:
    """Resolved operational config for one companion (never None on the hot path)."""

    model: str | None = None
    temperature: float = _DEFAULT_TEMPERATURE
    tool_allow: frozenset[str] = field(default_factory=frozenset)
    tool_deny: frozenset[str] = field(default_factory=frozenset)
    allow_body_control: bool = True
    max_tool_iters: int | None = None

    @classmethod
    def parse(cls, raw: object) -> CompanionRuntimeConfig:
        if not isinstance(raw, dict):
            return cls()
        model = raw.get("model")
        model = model.strip() if isinstance(model, str) and model.strip() else None

        temp = raw.get("temperature")
        temperature = float(temp) if isinstance(temp, (int, float)) else _DEFAULT_TEMPERATURE

        tools = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
        tool_allow = _str_set(tools.get("allow"))
        tool_deny = _str_set(tools.get("deny"))
        abc = tools.get("allow_body_control")
        allow_body_control = abc if isinstance(abc, bool) else True

        mti = raw.get("max_tool_iters")
        max_tool_iters = mti if isinstance(mti, int) and mti > 0 else None

        return cls(
            model=model,
            temperature=temperature,
            tool_allow=tool_allow,
            tool_deny=tool_deny,
            allow_body_control=allow_body_control,
            max_tool_iters=max_tool_iters,
        )


def _str_set(value: object) -> frozenset[str]:
    if isinstance(value, (list, tuple, set)):
        return frozenset(str(x).strip() for x in value if str(x).strip())
    return frozenset()


class CompanionConfigResolver:
    """Resolve ``CompanionRuntimeConfig`` per companion, TTL-cached.

    Reads ``companions.runtime_config_json`` from eidolon_data. The per-companion
    ``TurnEngine`` is cached with no eviction, so config is resolved *per turn*
    through this cache (default 5s TTL) — admin edits land within the TTL without
    a process restart, and a DB hit only happens on a cache miss (off the hot path
    otherwise).
    """

    def __init__(self, data_store: object, *, ttl_s: float = 5.0) -> None:
        self._data_store = data_store
        self._ttl_s = ttl_s
        self._cache: dict[str, tuple[float, CompanionRuntimeConfig]] = {}

    async def resolve(self, companion_id: str | None) -> CompanionRuntimeConfig:
        if not companion_id:
            return CompanionRuntimeConfig()
        now = time.monotonic()
        hit = self._cache.get(companion_id)
        if hit is not None and (now - hit[0]) < self._ttl_s:
            return hit[1]
        cfg = await self._fetch(companion_id)
        self._cache[companion_id] = (now, cfg)
        return cfg

    async def _fetch(self, companion_id: str) -> CompanionRuntimeConfig:
        companions = getattr(self._data_store, "companions", None)
        get = getattr(companions, "get", None)
        if get is None:
            return CompanionRuntimeConfig()
        try:
            row = await get(companion_id)
        except Exception as exc:  # config must never break a turn
            _log.warning("companion runtime_config fetch failed for %s: %s", companion_id, exc)
            return CompanionRuntimeConfig()
        if row is None:
            return CompanionRuntimeConfig()
        return CompanionRuntimeConfig.parse(getattr(row, "runtime_config_json", None))

    def invalidate(self, companion_id: str | None = None) -> None:
        if companion_id is None:
            self._cache.clear()
        else:
            self._cache.pop(companion_id, None)
