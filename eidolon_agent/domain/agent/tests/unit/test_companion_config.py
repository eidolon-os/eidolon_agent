"""P2: per-companion runtime config parsing + TTL-cached resolution."""

from __future__ import annotations

from eidolon_agent.domain.agent.companion_config import (
    CompanionConfigResolver,
    CompanionRuntimeConfig,
)


def test_parse_defaults_on_empty_or_non_dict():
    assert CompanionRuntimeConfig.parse(None) == CompanionRuntimeConfig()
    assert CompanionRuntimeConfig.parse("nope") == CompanionRuntimeConfig()
    assert CompanionRuntimeConfig.parse({"unknown_key": 1}) == CompanionRuntimeConfig()


def test_parse_reads_model_temperature_and_tools():
    cfg = CompanionRuntimeConfig.parse(
        {
            "model": "  openai/deepseek-v4-flash  ",
            "temperature": 0.2,
            "tools": {"allow": ["get_time", "  "], "deny": ["reboot"], "allow_body_control": False},
            "max_tool_iters": 6,
        }
    )
    assert cfg.model == "openai/deepseek-v4-flash"  # trimmed
    assert cfg.temperature == 0.2
    assert cfg.tool_allow == frozenset({"get_time"})  # blank dropped
    assert cfg.tool_deny == frozenset({"reboot"})
    assert cfg.allow_body_control is False
    assert cfg.max_tool_iters == 6


def test_parse_is_defensive_about_bad_types():
    cfg = CompanionRuntimeConfig.parse(
        {"model": 123, "temperature": "hot", "tools": "x", "max_tool_iters": -3}
    )
    assert cfg.model is None
    assert cfg.temperature == 0.7
    assert cfg.tool_allow == frozenset()
    assert cfg.tool_deny == frozenset()
    assert cfg.allow_body_control is True
    assert cfg.max_tool_iters is None


class _Row:
    def __init__(self, runtime_config_json):
        self.runtime_config_json = runtime_config_json


class _Companions:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    async def get(self, companion_id):
        self.calls += 1
        return self._rows.get(companion_id)


class _DataStore:
    def __init__(self, rows):
        self.companions = _Companions(rows)


async def test_resolver_reads_then_caches_then_invalidates():
    ds = _DataStore({"c1": _Row({"model": "m1"})})
    resolver = CompanionConfigResolver(ds, ttl_s=100.0)

    first = await resolver.resolve("c1")
    second = await resolver.resolve("c1")
    assert first.model == "m1"
    assert second is first  # cache returns the same resolved config object
    assert ds.companions.calls == 1  # second served from cache

    resolver.invalidate("c1")
    await resolver.resolve("c1")
    assert ds.companions.calls == 2  # re-fetched after invalidate


async def test_resolver_handles_none_id_and_missing_companion():
    ds = _DataStore({})
    resolver = CompanionConfigResolver(ds)
    assert await resolver.resolve(None) == CompanionRuntimeConfig()
    assert await resolver.resolve("missing") == CompanionRuntimeConfig()


async def test_resolver_never_raises_on_store_error():
    class _BadCompanions:
        async def get(self, companion_id):
            raise RuntimeError("db down")

    class _BadStore:
        companions = _BadCompanions()

    resolver = CompanionConfigResolver(_BadStore())
    assert await resolver.resolve("c1") == CompanionRuntimeConfig()
