"""Read-only shadow runner fed by an owner-scoped device directory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Protocol

from .contracts import DeviceOption, InterpretationPort, InterpretationRequest, Suggestion

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DirectoryDevice:
    """Owner-scoped smart-home directory projection from its authority."""

    device_id: str
    name: str
    room: str
    kind: str
    online: bool


class DeviceDirectoryPort(Protocol):
    async def list_devices(
        self,
        *,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None = None,
    ) -> list[DirectoryDevice]: ...


class ShadowInterpreter:
    def __init__(self, *, directory: DeviceDirectoryPort, adapter: InterpretationPort) -> None:
        self.directory = directory
        self.adapter = adapter

    async def observe(
        self,
        *,
        utterance: str,
        turn_id: str,
        owner_id: str,
        companion_id: str,
        source_device_id: str | None = None,
    ) -> Suggestion | None:
        try:
            devices = await asyncio.wait_for(
                self.directory.list_devices(
                    owner_id=owner_id, companion_id=companion_id, source_device_id=source_device_id
                ),
                timeout=0.5,
            )
            candidates = tuple(
                DeviceOption(d.device_id, d.name.strip(), d.room, d.kind)
                for d in devices
                if d.online and d.name.strip()
            )
            request = InterpretationRequest(utterance, candidates, turn_id, owner_id)
            result = await self.adapter.interpret(request)
        except Exception:
            # Shadow inference never affects the user turn or command path.
            result = None
        _log.info(
            "home_interpretation_shadow %s",
            json.dumps(
                {
                    "turn_id": turn_id,
                    "utterance_sha256": hashlib.sha256(utterance.encode()).hexdigest(),
                    "suggestion": None
                    if result is None
                    else {
                        "intent": result.intent,
                        "device_id": result.device_id,
                        "action": result.action,
                        "confidence": result.confidence,
                        "source": result.source,
                        "model_revision": result.model_revision,
                    },
                },
                ensure_ascii=False,
            ),
        )
        return result
