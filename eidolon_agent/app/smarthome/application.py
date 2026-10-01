"""Compose one Owner-scoped smart-home command use case and its model adapter."""

from __future__ import annotations

import logging
import os
import time

from eidolon_sdk.biz.smarthome import HomeSessionScope, VoiceResult

from eidolon_agent.app.smarthome.sessions import HomeSessions, HomeSessionUnavailable
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.ports.runtime_authority import CompanionRuntimeAuthority
from eidolon_agent.domain.interpretation import InterpretationConfig, InterpretationService
from eidolon_agent.domain.runtime_session import RuntimeSessionAuthorizer
from eidolon_agent.domain.smarthome import SmartHomeCommand
from eidolon_agent.infra.interpretation import (
    JsonlInterpretationRecorder,
    LayaInterpreter,
    RulesInterpreter,
)
from eidolon_agent.infra.smarthome import HubSmartHomeClient, LlmHomeFallback
from eidolon_agent.infra.smarthome.laya_continuation import MODEL_REVISION, LayaHomeContinuation

_log = logging.getLogger(__name__)


class SmartHomeApplication:
    """Owns interpretation and command policy; transports only carry its result."""

    def __init__(
        self,
        command: SmartHomeCommand,
        *,
        interpreter: InterpretationService | RulesInterpreter,
        runtime_authority: CompanionRuntimeAuthority,
        laya: LayaInterpreter | None = None,
        hub_client: HubSmartHomeClient | None = None,
    ) -> None:
        self._command = command
        self._interpreter = interpreter
        self._laya = laya
        self._hub_client = hub_client
        self._sessions = HomeSessions()
        self._runtime_sessions = RuntimeSessionAuthorizer(runtime_authority)

    async def handle(
        self, scope: HomeSessionScope, turn_id: str, utterance: str,
    ) -> VoiceResult:
        start = time.monotonic()
        session = self._sessions.get(scope)
        # Invalidate old interpretation as soon as a new input arrives, before
        # either an authority read or serialized context updates can await.
        session.context.revision += 1
        revision = session.context.revision
        # The same authority as Companion conversations, without entering their
        # prompt compiler, memory or TurnEngine. Check active identity each turn.
        authority_started = time.monotonic()
        await self._runtime_sessions.authorize(
            owner_id=scope.owner_id, companion_id=scope.companion_id,
            device_id=scope.device_ref, session_id=scope.session_id,
        )
        authority_ms = int((time.monotonic() - authority_started) * 1000)
        async with session.lock:
            if not session.context.active:
                raise HomeSessionUnavailable("home session is closed")
            result = await self._command.handle(
                scope.owner_id, scope.device_ref, turn_id, utterance,
                context=session.context, revision=revision,
            )
        _log.info("home turn=%s session=%s companion=%s authority_ms=%d elapsed_ms=%d result=%s",
                  turn_id, scope.session_id, scope.companion_id,
                  authority_ms,
                  (time.monotonic() - start) * 1000, result.model_dump_json())
        return result

    def end_session(self, scope: HomeSessionScope) -> None:
        self._sessions.close(scope)

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


def build_smart_home_application(
    llm: LLMPort | None = None, *, runtime_authority: CompanionRuntimeAuthority,
) -> SmartHomeApplication | None:
    """Build at Agent startup, separately from the Companion and Admin apps."""
    token = os.environ.get("EIDOLON_HUB_SMARTHOME_TOKEN", "")
    if len(token) < 32:
        return None
    interpreter_name = os.environ.get("EIDOLON_SMARTHOME_INTERPRETER", "rules").strip().lower()
    continuation_revision = os.environ.get("EIDOLON_SMARTHOME_LAYA_CONTINUATION_REVISION", "").strip()
    if continuation_revision and (
        continuation_revision != MODEL_REVISION or interpreter_name != "laya"
        or llm is None or llm.model_id == "fake"
    ):
        raise ValueError("Laya continuation requires the supported revision, Laya mode and a real LLM")
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
    continuation = LayaHomeContinuation(laya, revision=continuation_revision) if continuation_revision else None
    return SmartHomeApplication(
        SmartHomeCommand(
            directory=client, executor=client, interpreter=interpreter,
            fallback=fallback, min_confidence=min_confidence, continuation=continuation,
            independent_interpreter=RulesInterpreter(require_complete=True),
        ),
        interpreter=interpreter,
        runtime_authority=runtime_authority,
        laya=laya,
        hub_client=client,
    )
