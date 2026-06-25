"""Caller / tenant identity.

Built once per gRPC stream by the auth interceptor (or per HTTP request by the
FastAPI dependency). Immutable after that — downstream code treats it as
opaque routing key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eidolon_sdk.memory import MemoryActorContext, derive_memory_space_id

# Fields the agent does not yet resolve (it has no first-class persona_id /
# agent_id / instance_id concept on the caller identity) fall back to this
# non-blank sentinel so the SDK's MemoryActorContext non-blank contract holds.
_UNRESOLVED = "default"


def build_memory_space_id(
    *,
    user_id: str,
    tenant_id: str | None = None,
    companion_id: str | None = None,
    persona_id: str | None = None,
) -> str:
    """Derive the ``<tenant_id>.<owner_user_id>.<companion_id>`` memory-space id."""
    resolved_companion_id = _resolve_companion_id(
        companion_id=companion_id,
        persona_id=persona_id,
    )
    return derive_memory_space_id(
        tenant_id or _UNRESOLVED,
        user_id,
        resolved_companion_id,
    )


def build_memory_actor_context(
    *,
    user_id: str,
    session_id: str,
    tenant_id: str | None = None,
    companion_id: str | None = None,
    persona_id: str | None = None,
    agent_id: str | None = None,
    device_id: str | None = None,
    instance_id: str | None = None,
) -> MemoryActorContext:
    """Build the actor context carried on every memory read/write.

    Producers pass whatever identity they hold (``tenant_id`` / ``device_id`` /
    ``agent_instance_id`` from the :class:`Identity`); unresolved fields fall
    back to ``"default"``. Product code should pass ``companion_id``; the SDK
    wire field remains ``persona_id``.
    """
    resolved_companion_id = _resolve_companion_id(
        companion_id=companion_id,
        persona_id=persona_id,
    )
    return MemoryActorContext(
        tenant_id=tenant_id or _UNRESOLVED,
        owner_user_id=user_id,
        persona_id=resolved_companion_id,
        agent_id=agent_id or _UNRESOLVED,
        device_id=device_id or _UNRESOLVED,
        instance_id=instance_id or _UNRESOLVED,
        session_id=session_id,
    )


def _resolve_companion_id(
    *,
    companion_id: str | None = None,
    persona_id: str | None = None,
) -> str:
    if companion_id and persona_id and companion_id != persona_id:
        raise ValueError("companion_id and persona_id must match when both are provided")
    return companion_id or persona_id or _UNRESOLVED


class CallerKind(str, Enum):
    """Where a call originated. Drives style and policy branches downstream."""

    LIVEKIT_VOICE = "livekit_voice"
    WEB_CHAT = "web_chat"
    IOT_TERMINAL = "iot_terminal"
    DESKTOP_UI = "desktop_ui"
    ADMIN_TEST = "admin_test"
    INTERNAL = "internal"  # internal subsystems (proactive engine, schedulers)


@dataclass(frozen=True, slots=True)
class Identity:
    """Stable identity tuple. Used as routing / metric label / storage prefix."""

    tenant_id: str
    user_id: str
    agent_instance_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class CallerContext:
    """Per-call context carried through the entire Turn pipeline."""

    identity: Identity
    caller_kind: CallerKind
    trace_id: str
    request_id: str
    locale: str = "zh-CN"

    @property
    def tenant_id(self) -> str:
        return self.identity.tenant_id

    @property
    def user_id(self) -> str:
        return self.identity.user_id

    @property
    def agent_instance_id(self) -> str | None:
        return self.identity.agent_instance_id
