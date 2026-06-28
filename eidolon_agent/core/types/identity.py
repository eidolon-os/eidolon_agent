"""Runtime caller identity.

Built once per gRPC stream by the auth interceptor. Downstream code treats it
as the routing key for companion runtime, history, and memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eidolon_sdk.memory import MemoryActorContext, derive_memory_space_id


def build_memory_space_id(*, memory_realm_id: str) -> str:
    """Use the memory realm as the memory-space id."""

    return derive_memory_space_id(memory_realm_id)


def build_memory_actor_context(
    *,
    owner_id: str,
    companion_id: str,
    memory_realm_id: str,
    device_id: str,
    session_id: str,
) -> MemoryActorContext:
    """Build the actor context carried on every memory read/write."""

    return MemoryActorContext(
        owner_id=owner_id,
        companion_id=companion_id,
        memory_realm_id=memory_realm_id,
        device_id=device_id,
        session_id=session_id,
    )


class CallerKind(str, Enum):
    """Where a call originated. Drives style and policy branches downstream."""

    LIVEKIT_VOICE = "livekit_voice"
    WEB_CHAT = "web_chat"
    IOT_TERMINAL = "iot_terminal"
    DESKTOP_UI = "desktop_ui"
    ADMIN_TEST = "admin_test"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class Identity:
    """Stable owner/companion/device identity tuple."""

    owner_id: str
    companion_id: str
    device_id: str
    memory_realm_id: str
    genome_id: str


@dataclass(frozen=True, slots=True)
class CallerContext:
    """Per-call context carried through the entire turn pipeline."""

    identity: Identity
    caller_kind: CallerKind
    trace_id: str
    request_id: str
    locale: str = "zh-CN"

    @property
    def owner_id(self) -> str:
        return self.identity.owner_id

    @property
    def companion_id(self) -> str:
        return self.identity.companion_id

    @property
    def device_id(self) -> str:
        return self.identity.device_id

    @property
    def memory_realm_id(self) -> str:
        return self.identity.memory_realm_id

    @property
    def genome_id(self) -> str:
        return self.identity.genome_id
