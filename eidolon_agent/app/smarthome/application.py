"""Compose one Owner-scoped smart-home command use case and its model adapter."""

from __future__ import annotations

import logging
import os
import time

from eidolon_sdk.biz.smarthome import HomeSessionScope, VoiceResult

from eidolon_agent.app.smarthome.sessions import HomeSessions, HomeSessionUnavailable
from eidolon_agent.config.settings import SmartHomeSettings
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

_log = logging.getLogger(__name__)


class SmartHomeApplication:
    """Owns interpretation and command policy; transports only carry its result."""

    def __init__(
        self,
        command: SmartHomeCommand,
        *,
        interpreter: InterpretationService | RulesInterpreter,
        runtime_authority: CompanionRuntimeAuthority,
        context_turns: int = 3,
        laya: LayaInterpreter | None = None,
        hub_client: HubSmartHomeClient | None = None,
    ) -> None:
        self._command = command
        self._interpreter = interpreter
        self._laya = laya
        self._hub_client = hub_client
        self._sessions = HomeSessions(context_turns=context_turns)
        self._runtime_sessions = RuntimeSessionAuthorizer(runtime_authority)

    async def handle(
        self, scope: HomeSessionScope, turn_id: str, utterance: str,
    ) -> VoiceResult:
        start = time.monotonic()
        session = self._sessions.get(scope)
        # FIFO per session: a second complete command does not cancel an earlier
        # independent command just because ASR produced it before the first finished.
        # Closing the session still invalidates all in-flight interpretation.
        queued = time.monotonic()
        async with session.lock:
            _log.info("home stage turn=%s stage=queue elapsed_ms=%.1f", turn_id, (time.monotonic() - queued) * 1000)
            if not session.context.active:
                raise HomeSessionUnavailable("home session is closed")
            session.context.revision += 1
            revision = session.context.revision
            authority_started = time.monotonic()
            await self._runtime_sessions.authorize(
                owner_id=scope.owner_id, companion_id=scope.companion_id,
                device_id=scope.device_ref, session_id=scope.session_id,
            )
            authority_ms = int((time.monotonic() - authority_started) * 1000)
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
    llm: LLMPort | None = None,
    *,
    runtime_authority: CompanionRuntimeAuthority,
    settings: SmartHomeSettings | None = None,
) -> SmartHomeApplication | None:
    """Build at Agent startup, separately from the Companion and Admin apps.

    ``settings`` is agent.yaml's ``smarthome``; where the Laya model answers (``laya.url``) is the
    one thing about it Ops writes per Host. The Hub token is a credential and stays in agent.env.
    """
    settings = settings or SmartHomeSettings()
    token = os.environ.get("EIDOLON_HUB_SMARTHOME_TOKEN", "")
    if len(token) < 32:
        return None
    real_llm = llm is not None and llm.model_id != "fake"
    laya = None
    if settings.interpreter == "laya":
        laya = LayaInterpreter(settings.laya.url, api_key=settings.laya.token or None)
        record_path = settings.interpretation_record_path
        interpreter = InterpretationService(
            {"laya": laya},
            InterpretationConfig(primary="laya"),
            recorder=JsonlInterpretationRecorder(record_path) if record_path else None,
        )
    else:
        interpreter = RulesInterpreter()
    client = HubSmartHomeClient(base_url=settings.hub_url, token=token)
    fallback = LlmHomeFallback(llm) if real_llm else None
    min_confidence = settings.laya.min_confidence if fallback and laya is not None else 0.0
    return SmartHomeApplication(
        SmartHomeCommand(
            directory=client, executor=client, interpreter=interpreter,
            fallback=fallback, min_confidence=min_confidence,
        ),
        interpreter=interpreter,
        runtime_authority=runtime_authority,
        laya=laya,
        hub_client=client,
        context_turns=settings.context_turns,
    )
