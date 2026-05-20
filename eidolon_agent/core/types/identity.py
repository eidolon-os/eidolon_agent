"""Caller / tenant identity.

Built once per gRPC stream by the auth interceptor (or per HTTP request by the
FastAPI dependency). Immutable after that — downstream code treats it as
opaque routing key.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
