"""Per-companion operational configuration from the Runtime Authority port.

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

    Reads the versioned System Data runtime snapshot. The per-companion
    ``TurnEngine`` is cached with no eviction, so config is resolved *per turn*
    through this cache (default 5s TTL) — admin edits land within the TTL without
    a process restart, and a DB hit only happens on a cache miss (off the hot path
    otherwise).
    """

    def __init__(self, runtime_authority: object, *, ttl_s: float = 5.0) -> None:
        self._runtime_authority = runtime_authority
        self._ttl_s = ttl_s
        self._cache: dict[tuple[str, str], tuple[float, CompanionRuntimeConfig]] = {}

    async def resolve(
        self,
        owner_id: str | None,
        companion_id: str | None,
    ) -> CompanionRuntimeConfig:
        if not owner_id or not companion_id:
            return CompanionRuntimeConfig()
        key = (owner_id, companion_id)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and (now - hit[0]) < self._ttl_s:
            return hit[1]
        cfg = await self._fetch(owner_id, companion_id)
        self._cache[key] = (now, cfg)
        return cfg

    async def _fetch(self, owner_id: str, companion_id: str) -> CompanionRuntimeConfig:
        try:
            facts = await self._runtime_authority.resolve(
                owner_id=owner_id,
                companion_id=companion_id,
            )
        except Exception as exc:  # config must never break a turn
            _log.warning("companion runtime_config fetch failed for %s: %s", companion_id, exc)
            return CompanionRuntimeConfig()
        return CompanionRuntimeConfig.parse(facts.runtime_config)

    def invalidate(
        self,
        *,
        owner_id: str | None = None,
        companion_id: str | None = None,
    ) -> None:
        if owner_id is None and companion_id is None:
            self._cache.clear()
            return
        for key in list(self._cache):
            if owner_id is not None and key[0] != owner_id:
                continue
            if companion_id is not None and key[1] != companion_id:
                continue
            self._cache.pop(key, None)
