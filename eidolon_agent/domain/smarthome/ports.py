"""Ports for the smart-home domain.

Implemented later by a Capability Runtime client (Channel Provider process):
the directory is its Owner-scoped projection of the System Data registry plus
live Provider state, and execute is its idempotent, deadline-bound submit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from eidolon_sdk.biz.interpretation import InterpretationRequest, Proposal
from eidolon_sdk.biz.smarthome import ExecuteRequest, ExecuteResult, Registry, StateValue


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    """What the device's Provider last reported."""

    online: bool
    state: Mapping[str, StateValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HomeSnapshot:
    """The Owner's registry and each device's live status.

    A device missing from ``status`` has no known state; it is not assumed
    offline or idle.
    """

    registry: Registry
    status: Mapping[str, DeviceStatus] = field(default_factory=dict)


class SmartHomeDirectoryPort(Protocol):
    async def snapshot(self, owner_id: str) -> HomeSnapshot:
        """The current home. Raises :class:`SmartHomeUnavailable`."""
        ...


class SmartHomeExecutePort(Protocol):
    async def execute(self, owner_id: str, request: ExecuteRequest) -> ExecuteResult:
        """Submit commands; a repeated ``request_id`` returns the first result.

        Raises :class:`SmartHomeUnavailable` only when the request was certainly
        not accepted. A command still running at the deadline is ``unknown``.
        """
        ...


class SmartHomeFallbackPort(Protocol):
    async def propose(self, request: InterpretationRequest) -> Proposal | None:
        """Asked when the interpreter abstains or fails (e.g. an LLM holding only
        the smart-home tools, for 有点热). ``None`` means still not understood.
        May raise ``InterpretationError``; the proposal is validated like any other.
        """
        ...
