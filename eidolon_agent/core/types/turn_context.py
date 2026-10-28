"""Owner-scoped facts required to execute one Agent turn.

The context does not introduce another principal. ``owner_id`` is the sole
security/namespace principal; the remaining fields pin the runtime target,
optional source Device, locale, and correlation identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from eidolon_memory_contracts import MemoryActorContext, derive_memory_space_id
from eidolon_sdk.biz.speaker_identity import SpeakerIdentityObservation

InputModality = Literal["voice", "text"]


def build_memory_space_id(*, memory_realm_id: str) -> str:
    """Use the memory realm as the memory-space id."""

    return derive_memory_space_id(memory_realm_id)


def build_memory_actor_context(
    *,
    memory_realm_id: str,
    person_id: str | None = None,
    identity_epoch: int | None = None,
    owner_id: str | None = None,
    companion_id: str | None = None,
    council_id: str | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
) -> MemoryActorContext:
    """Map Agent facts to the existing Memory contract."""

    return MemoryActorContext(
        person_id=person_id,
        identity_epoch=identity_epoch,
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
    person_id: str | None = None
    identity_epoch: int | None = None
    person_revision: int | None = None
    schema_version: str = ""
    genome_hash: str = ""
    realizer_version: str = ""
    locale: str = "zh-CN"
    # Internal Owner tasks retain their policy; authenticated interactive sessions
    # explicitly derive this from their authorized Person binding.
    memory_enabled: bool = True
    speaker_observation: SpeakerIdentityObservation | None = None
    source_session_id: str | None = None

    # Internal authority only. Does not enable canonical reads, tools or commitments.
    provisional_memory_save: bool = False
    provisional_memory_recall: bool = False

    def __post_init__(self) -> None:
        if (type(self.provisional_memory_save) is not bool
                or type(self.provisional_memory_recall) is not bool):
            raise ValueError("provisional save permission must be boolean")
        if (self.provisional_memory_save or self.provisional_memory_recall) and (
            self.memory_enabled or not self.owner_id or not self.person_id
            or type(self.identity_epoch) is not int or self.identity_epoch < 0
            or type(self.person_revision) is not int or self.person_revision < 1
            or not self.source_session_id
        ):
            raise ValueError("provisional save requires an isolated, bound Person session")

    @property
    def memory_write_enabled(self) -> bool:
        return self.memory_enabled or self.provisional_memory_save
