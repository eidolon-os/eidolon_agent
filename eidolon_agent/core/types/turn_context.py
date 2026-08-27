"""Owner-scoped facts required to execute one Agent turn.

The context does not introduce another principal. ``owner_id`` is the sole
security/namespace principal; the remaining fields pin the runtime target,
optional source Device, locale, and correlation identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from eidolon_memory_contracts import MemoryActorContext, derive_memory_space_id

InputModality = Literal["voice", "text"]


def build_memory_space_id(*, memory_realm_id: str) -> str:
    """Use the memory realm as the memory-space id."""

    return derive_memory_space_id(memory_realm_id)


def build_memory_actor_context(
    *,
    memory_realm_id: str,
    owner_id: str | None = None,
    companion_id: str | None = None,
    council_id: str | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
) -> MemoryActorContext:
    """Map Agent facts to the existing Memory contract."""

    return MemoryActorContext(
        owner_id=owner_id,
        companion_id=companion_id,
        council_id=council_id,
        memory_realm_id=memory_realm_id,
        device_id=device_id,
        session_id=session_id,
    )


@dataclass(frozen=True, slots=True)
class TurnContext:
    """Stable Owner scope, runtime pins, and correlation for one turn."""

    owner_id: str
    companion_id: str
    device_id: str | None
    memory_realm_id: str
    genome_id: str
    trace_id: str
    request_id: str
    schema_version: str = ""
    genome_hash: str = ""
    realizer_version: str = ""
    locale: str = "zh-CN"
