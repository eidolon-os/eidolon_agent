"""Companion operational config is strict authority input, not a fallback."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig


def test_authority_config_maps_known_operational_policy() -> None:
    cfg = CompanionRuntimeConfig.from_authority(
        {
            "model": "  openai/deepseek-v4-flash  ",
            "temperature": 0.2,
            "tools": {
                "allow": ["get_time"],
                "deny": ["reboot"],
                "allow_body_control": False,
            },
            "max_tool_iters": 6,
            "future_field": {"ignored": True},
        }
    )

    assert cfg.model == "openai/deepseek-v4-flash"
    assert cfg.temperature == 0.2
    assert cfg.tool_allow == frozenset({"get_time"})
    assert cfg.tool_deny == frozenset({"reboot"})
    assert cfg.allow_body_control is False
    assert cfg.max_tool_iters == 6


def test_empty_authority_config_uses_explicit_product_defaults() -> None:
    assert CompanionRuntimeConfig.from_authority({}) == CompanionRuntimeConfig()


@pytest.mark.parametrize(
    "raw, message",
    [
        (None, "must be an object"),
        ({"model": 123}, "model must be a string"),
        ({"temperature": "hot"}, "temperature must be numeric"),
        ({"tools": "all"}, "tools must be an object"),
        ({"tools": {"allow": [""]}}, "entries must be non-empty strings"),
        ({"tools": {"allow_body_control": "yes"}}, "must be boolean"),
        ({"max_tool_iters": 0}, "must be a positive integer"),
    ],
)
def test_malformed_known_policy_fails_closed(raw: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CompanionRuntimeConfig.from_authority(raw)
