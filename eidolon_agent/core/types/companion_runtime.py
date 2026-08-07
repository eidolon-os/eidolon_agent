"""Domain facts required to run one Companion instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eidolon_sdk.biz.persona import PersonaGenome


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


__all__ = ["CompanionRuntimeFacts"]
