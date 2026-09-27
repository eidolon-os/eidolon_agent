"""Agent-owned interpretation DTOs; no command capability crosses this port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

CONTRACT_VERSION = "eidolon.agent.interaction-interpretation.v1"
MODEL_CONTRACT_VERSION = "eidolon.models.laya.systemone.v1"
INTENTS = ("控制", "查询", "无关")
ACTIONS = (
    "打开或启动",
    "关闭或停止",
    "调高或增大",
    "调低或减小",
    "设为指定的数值或模式",
    "暂停",
    "上锁",
)


@dataclass(frozen=True, slots=True)
class DeviceOption:
    device_id: str
    label: str
    room: str = ""
    kind: str = ""


@dataclass(frozen=True, slots=True)
class InterpretationRequest:
    utterance: str
    devices: tuple[DeviceOption, ...]
    turn_id: str
    owner_id: str
    contract_version: str = CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError("unsupported interpretation contract")
        if not self.utterance.strip() or len(self.utterance) > 1024:
            raise ValueError("utterance must contain 1-1024 characters")
        if len(self.devices) > 30:
            raise ValueError("at most 30 candidate devices")
        labels = [item.label for item in self.devices]
        if len(set(labels)) != len(labels) or any(not label.strip() for label in labels):
            raise ValueError("candidate labels must be nonempty and unique")
        if len({item.device_id for item in self.devices}) != len(self.devices):
            raise ValueError("candidate device ids must be unique")


@dataclass(frozen=True, slots=True)
class Suggestion:
    intent: Literal["控制", "查询", "无关"]
    device_id: str | None
    action: str | None
    confidence: float
    source: str
    model_revision: str | None = None
    # This object deliberately has no executable op, payload or permission.


class InterpretationPort(Protocol):
    async def interpret(self, request: InterpretationRequest) -> Suggestion | None: ...
