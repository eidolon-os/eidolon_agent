"""Runtime caller identity.

Built once per gRPC stream by the auth interceptor. Downstream code treats it
as the routing key for companion runtime, history, and memory.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from eidolon_memory_contracts import MemoryActorContext, derive_memory_space_id


def build_memory_space_id(*, memory_realm_id: str) -> str:
    """Use the memory realm as the memory-space id."""

    return derive_memory_space_id(memory_realm_id)


def build_memory_actor_context(
    *,
    memory_realm_id: str,
    owner_id: str | None = None,
    companion_id: str | None = None,
    device_id: str | None = None,
    session_id: str | None = None,
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
    """Stable runtime identity tuple.

    ``device_id`` is present for physical/device-origin sessions and absent
    for owner/web/cloud-task entrances.
    """

    owner_id: str
    companion_id: str
    device_id: str | None
    memory_realm_id: str
    genome_id: str
    schema_version: str = ""
    genome_hash: str = ""
    realizer_version: str = ""


@dataclass(frozen=True, slots=True)
class CallerContext:
    """Per-call context carried through the entire turn pipeline."""

    identity: Identity
    caller_kind: CallerKind
    trace_id: str
    request_id: str
    runtime_caller_id: str | None = None
    runtime_session_id: str | None = None
    actor_kind: str = ""
    actor_id: str = ""
    display_name: str = ""
    transport: str = ""
    locale: str = "zh-CN"

    @property
    def owner_id(self) -> str:
        return self.identity.owner_id

    @property
    def companion_id(self) -> str:
        return self.identity.companion_id

    @property
    def device_id(self) -> str | None:
        return self.identity.device_id

    @property
    def memory_realm_id(self) -> str:
        return self.identity.memory_realm_id

    @property
    def genome_id(self) -> str:
        return self.identity.genome_id

    @property
    def genome_hash(self) -> str:
        return self.identity.genome_hash

    @property
    def schema_version(self) -> str:
        return self.identity.schema_version

    @property
    def realizer_version(self) -> str:
        return self.identity.realizer_version


def derive_runtime_caller_id(
    *,
    owner_id: str,
    companion_id: str,
    actor_kind: str,
    actor_id: str,
) -> str:
    digest = hashlib.sha256(
        "\0".join((owner_id, companion_id, actor_kind, actor_id)).encode()
    ).hexdigest()
    return f"rc_{digest[:24]}"
