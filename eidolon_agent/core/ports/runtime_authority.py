"""Port for the System Data Companion Runtime Authority."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from eidolon_agent.core.types.companion_runtime import CompanionRuntimeFacts


@runtime_checkable
class CompanionRuntimeAuthority(Protocol):
    async def resolve(
        self,
        *,
        owner_id: str,
        companion_id: str,
        genome_id: str | None = None,
    ) -> CompanionRuntimeFacts: ...


__all__ = ["CompanionRuntimeAuthority"]
