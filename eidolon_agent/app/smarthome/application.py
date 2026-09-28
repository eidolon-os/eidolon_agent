"""Compose one Owner-scoped smart-home command use case and its model adapter."""

from __future__ import annotations

import os

from eidolon_sdk.biz.smarthome import VoiceResult

from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.domain.interpretation import InterpretationConfig, InterpretationService
from eidolon_agent.domain.smarthome import SmartHomeCommand
from eidolon_agent.infra.interpretation import (
    JsonlInterpretationRecorder,
    LayaInterpreter,
    RulesInterpreter,
)
from eidolon_agent.infra.smarthome.channel import ChannelSmartHomeClient
from eidolon_agent.infra.smarthome.llm_fallback import LlmHomeFallback


class SmartHomeApplication:
    """Owns interpretation and command policy; transports only carry its result."""

    def __init__(
        self,
        command: SmartHomeCommand,
        *,
        interpreter: InterpretationService | RulesInterpreter,
        laya: LayaInterpreter | None = None,
    ) -> None:
        self._command = command
        self._interpreter = interpreter
        self._laya = laya

    async def handle(
        self, owner_id: str, device_ref: str, turn_id: str, utterance: str
    ) -> VoiceResult:
        return await self._command.handle(owner_id, device_ref, turn_id, utterance)

    async def close(self) -> None:
        if isinstance(self._interpreter, InterpretationService):
            await self._interpreter.drain()
        if self._laya is not None:
            await self._laya.aclose()


def build_smart_home_application(llm: LLMPort | None = None) -> SmartHomeApplication | None:
    """Build at Agent startup, separately from the Companion and Admin apps."""
    token = os.environ.get("EIDOLON_CHANNEL_PROVIDER_TOKEN", "")
    if len(token) < 32:
        return None
    interpreter_name = os.environ.get("EIDOLON_SMARTHOME_INTERPRETER", "rules").strip().lower()
    laya = None
    if interpreter_name == "laya":
        laya = LayaInterpreter(
            os.environ.get("EIDOLON_SMARTHOME_LAYA_URL", "http://127.0.0.1:8771")
        )
        record_path = os.environ.get("EIDOLON_SMARTHOME_INTERPRETATION_RECORD_PATH", "")
        interpreter = InterpretationService(
            {"laya": laya},
            InterpretationConfig(primary="laya"),
            recorder=JsonlInterpretationRecorder(record_path) if record_path else None,
        )
    elif interpreter_name == "rules":
        interpreter = RulesInterpreter()
    else:
        raise ValueError("EIDOLON_SMARTHOME_INTERPRETER must be rules or laya")
    client = ChannelSmartHomeClient(
        base_url=os.environ.get("EIDOLON_CHANNEL_PROVIDER_URL", "http://127.0.0.1:8767"),
        token=token,
    )
    fallback = LlmHomeFallback(llm) if llm is not None and llm.model_id != "fake" else None
    min_confidence = (
        float(os.environ.get("EIDOLON_SMARTHOME_MIN_CONFIDENCE", "0.8"))
        if fallback and interpreter_name == "laya" else 0.0
    )
    return SmartHomeApplication(
        SmartHomeCommand(
            directory=client, executor=client, interpreter=interpreter,
            fallback=fallback, min_confidence=min_confidence,
        ),
        interpreter=interpreter,
        laya=laya,
    )
