"""Compose one Owner-scoped smart-home command use case and its model adapter."""

from __future__ import annotations

import logging
import os
import time

from eidolon_sdk.biz.smarthome import VoiceResult

from eidolon_agent.app.smarthome.sessions import HomeSessions, HomeSessionUnavailable
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.domain.interpretation import InterpretationConfig, InterpretationService
from eidolon_agent.domain.smarthome import SmartHomeCommand
from eidolon_agent.infra.interpretation import (
    JsonlInterpretationRecorder,
    LayaInterpreter,
    RulesInterpreter,
)
from eidolon_agent.infra.smarthome import HubSmartHomeClient, LlmHomeFallback

_log = logging.getLogger(__name__)


class SmartHomeApplication:
    """Owns interpretation and command policy; transports only carry its result."""

    def __init__(
        self,
        command: SmartHomeCommand,
        *,
        interpreter: InterpretationService | RulesInterpreter,
        laya: LayaInterpreter | None = None,
        hub_client: HubSmartHomeClient | None = None,
    ) -> None:
        self._command = command
        self._interpreter = interpreter
        self._laya = laya
        self._hub_client = hub_client
        self._sessions = HomeSessions()

    async def handle(
        self, owner_id: str, device_ref: str, turn_id: str, utterance: str,
        *, session_id: str | None = None,
    ) -> VoiceResult:
        start = time.monotonic()
        # Older callers remain single-turn; never infer a session from device id.
        if session_id is None:
            result = await self._command.handle(owner_id, device_ref, turn_id, utterance)
        else:
            session = self._sessions.get(owner_id, device_ref, session_id)
            # Invalidate in-flight interpretation before waiting for serialized state updates.
            session.context.revision += 1
            revision = session.context.revision
            async with session.lock:
                if not session.context.active:
                    raise HomeSessionUnavailable("home session is closed")
                result = await self._command.handle(
                    owner_id, device_ref, turn_id, utterance, context=session.context, revision=revision,
                )
        _log.info("home turn=%s session=%s elapsed_ms=%d result=%s", turn_id, session_id,
                  (time.monotonic() - start) * 1000, result.model_dump_json())
        return result

    def end_session(self, owner_id: str, device_ref: str, session_id: str) -> None:
        self._sessions.close(owner_id, device_ref, session_id)

    async def close(self) -> None:
        self._sessions.clear()
        if isinstance(self._interpreter, InterpretationService):
            await self._interpreter.drain()
        try:
            if self._laya is not None:
                await self._laya.aclose()
        finally:
            if self._hub_client is not None:
                await self._hub_client.aclose()


def build_smart_home_application(llm: LLMPort | None = None) -> SmartHomeApplication | None:
    """Build at Agent startup, separately from the Companion and Admin apps."""
    token = os.environ.get("EIDOLON_HUB_SMARTHOME_TOKEN", "")
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
    client = HubSmartHomeClient(
        base_url=os.environ.get("EIDOLON_SMARTHOME_HUB_URL", "http://127.0.0.1:8082"),
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
            independent_interpreter=RulesInterpreter(require_complete=True),
        ),
        interpreter=interpreter,
        laya=laya,
        hub_client=client,
    )
