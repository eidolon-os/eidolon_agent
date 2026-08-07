"""Domain facts required to run one Companion instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eidolon_sdk.biz.persona import PersonaGenome

_DEFAULT_TEMPERATURE = 0.7


@dataclass(frozen=True, slots=True)
class CompanionRuntimeConfig:
    """Validated operational policy pinned to one authenticated session."""

    model: str | None = None
    temperature: float = _DEFAULT_TEMPERATURE
    tool_allow: frozenset[str] = frozenset()
    tool_deny: frozenset[str] = frozenset()
    allow_body_control: bool = True
    max_tool_iters: int | None = None

    @classmethod
    def from_authority(cls, raw: object) -> CompanionRuntimeConfig:
        """Map an authority-owned document without permissive type coercion."""

        if not isinstance(raw, dict):
            raise ValueError("runtime_config must be an object")

        model_value = raw.get("model")
        if model_value is not None and not isinstance(model_value, str):
            raise ValueError("runtime_config.model must be a string")
        model = model_value.strip() if isinstance(model_value, str) else None
        model = model or None

        temperature_value = raw.get("temperature", _DEFAULT_TEMPERATURE)
        if isinstance(temperature_value, bool) or not isinstance(
            temperature_value,
            (int, float),
        ):
            raise ValueError("runtime_config.temperature must be numeric")
        temperature = float(temperature_value)

        tools_value = raw.get("tools", {})
        if not isinstance(tools_value, dict):
            raise ValueError("runtime_config.tools must be an object")
        tool_allow = _identifier_set(tools_value.get("allow"), field_name="tools.allow")
        tool_deny = _identifier_set(tools_value.get("deny"), field_name="tools.deny")

        body_control_value = tools_value.get("allow_body_control", True)
        if not isinstance(body_control_value, bool):
            raise ValueError("runtime_config.tools.allow_body_control must be boolean")

        max_tool_iters_value = raw.get("max_tool_iters")
        if max_tool_iters_value is not None and (
            isinstance(max_tool_iters_value, bool)
            or not isinstance(max_tool_iters_value, int)
            or max_tool_iters_value <= 0
        ):
            raise ValueError("runtime_config.max_tool_iters must be a positive integer")

        return cls(
            model=model,
            temperature=temperature,
            tool_allow=tool_allow,
            tool_deny=tool_deny,
            allow_body_control=body_control_value,
            max_tool_iters=max_tool_iters_value,
        )


@dataclass(frozen=True, slots=True)
class CompanionRuntimeFacts:
    """Owner-scoped Data authority snapshot mapped out of its wire DTO."""

    owner_id: str
    companion_id: str
    memory_realm_id: str
    genome_id: str
    genome_version: int
    schema_version: str
    genome_hash: str
    realizer_version: str
    genome: PersonaGenome
    runtime_config: dict[str, Any]


def _identifier_set(value: object, *, field_name: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, (list, tuple, set)):
        raise ValueError(f"runtime_config.{field_name} must be an array")
    identifiers: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"runtime_config.{field_name} entries must be non-empty strings")
        identifiers.add(item.strip())
    return frozenset(identifiers)


__all__ = ["CompanionRuntimeConfig", "CompanionRuntimeFacts"]
