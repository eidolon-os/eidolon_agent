"""SmartHomeCommand — one spoken home command, from transcript to panel result.

Used by the companion-less command session (korvo-1 panel: ASR, one call, a
result card, no TTS) and, later, by a companion's ``TOOL_DIRECT`` triage. No
persona or memory is involved; the Owner comes from the trusted runtime scope.

    registry -> InterpretationRequest -> interpreter (-> LLM fallback if abstained)
      -> re-check against the current registry -> validated commands, or a scene id
      -> execute(idempotency key from turn_id, absolute deadline) -> VoiceResult
"""

from __future__ import annotations

import asyncio
import logging
import time

from eidolon_sdk.biz.interpretation import (
    ERROR_INVALID_PROPOSAL,
    ERROR_TIMEOUT,
    Area,
    Candidate,
    InterpretationError,
    InterpretationRequest,
    InterpretationResult,
    Origin,
    Proposal,
    validate_proposal,
)
from eidolon_sdk.biz.smarthome import (
    SCENE_KIND,
    Command,
    CommandTemplate,
    Device,
    PanelCandidate,
    Registry,
    SmartHomeError,
    VoiceOutcome,
    VoiceResult,
)
from eidolon_sdk.biz.smarthome import Origin as ExecuteOrigin

from eidolon_agent.core.ports.interpretation import InteractionInterpretationPort
from eidolon_agent.domain.smarthome.context import (
    HomeCancellation,
    HomeClarification,
    HomeContext,
    HomeUnderstanding,
)
from eidolon_agent.domain.smarthome.errors import SmartHomeUnavailable
from eidolon_agent.domain.smarthome.messages import (
    NO_SUCH_DEVICE,
    NOT_UNDERSTOOD,
    UNAVAILABLE,
    UNRELATED,
    choose_one,
    clip,
    describe,
    failure,
    not_found,
    summarize,
)
from eidolon_agent.domain.smarthome.planning import (
    CommandPlan,
    HomeActuator,
    device_command,
    find_target,
    plan_action,
    plan_devices,
    request_id,
)
from eidolon_agent.domain.smarthome.ports import (
    HomeSnapshot,
    SmartHomeDirectoryPort,
    SmartHomeExecutePort,
    SmartHomeFallbackPort,
)

_log = logging.getLogger(__name__)

MAX_CANDIDATES = 8  # VoiceResult shows at most this many choices
MAX_UTTERANCE = 512
MAX_SHOWN = 200


def interpretation_request(
    registry: Registry,
    *,
    interpretation_id: str,
    utterance: str,
    device_ref: str | None,
    timeout_ms: int,
) -> InterpretationRequest:
    """Devices and scenes as candidates; the speaker's room from its placement."""
    candidates = [
        Candidate(
            ref=device.device_id,
            name=device.name,
            aliases=device.aliases,
            kind=device.type,
            area_id=device.area_id,
        )
        for device in registry.devices
    ]
    candidates += [Candidate(ref=s.scene_id, name=s.name, kind=SCENE_KIND) for s in registry.scenes]
    return InterpretationRequest(
        interpretation_id=interpretation_id,
        domain="smarthome",
        utterance=utterance[:MAX_UTTERANCE],
        origin=Origin(
            device_ref=device_ref,
            area_id=registry.area_of(device_ref) if device_ref else None,
        ),
        candidates=tuple(candidates),
        areas=tuple(Area(area_id=a.area_id, name=a.name) for a in registry.areas),
        timeout_ms=timeout_ms,
    )


class SmartHomeCommand:
    def __init__(
        self,
        *,
        directory: SmartHomeDirectoryPort,
        executor: SmartHomeExecutePort,
        interpreter: InteractionInterpretationPort,
        fallback: SmartHomeFallbackPort | None = None,
        interpretation_timeout_ms: int = 800,
        execute_deadline_ms: int = 3000,
        fallback_timeout_s: float = 8.0,
        min_confidence: float = 0.0,
        independent_interpreter: InteractionInterpretationPort | None = None,
    ) -> None:
        self._directory = directory
        self._interpreter = interpreter
        self._fallback = fallback
        self._independent_interpreter = independent_interpreter
        self._interpretation_timeout_ms = interpretation_timeout_ms
        self._fallback_timeout_s = fallback_timeout_s
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self._min_confidence = min_confidence
        self._actuator = HomeActuator(executor, deadline_ms=execute_deadline_ms)

    async def handle(
        self, owner_id: str, device_ref: str | None, turn_id: str, utterance: str,
        *, context: HomeContext | None = None, revision: int | None = None,
    ) -> VoiceResult:
        context = context if context is not None else HomeContext()
        revision = context.revision if revision is None else revision
        def is_current() -> bool:
            return context.active and context.revision == revision
        previous = context.snapshot() if is_current() else None
        heard = " ".join(utterance.split())
        card = _Card(turn_id, heard[:MAX_SHOWN])
        if not is_current():
            return card("unavailable", "本轮已被新的输入替代或会话已结束")
        if not heard:
            return card("failed", NOT_UNDERSTOOD)
        try:
            home = await self._directory.snapshot(owner_id)
        except SmartHomeUnavailable:
            if is_current():
                context.clear()
            return card("unavailable", UNAVAILABLE)
        if not is_current():
            return card("unavailable", "本轮已被新的输入替代或会话已结束")
        request = interpretation_request(
            home.registry,
            interpretation_id=turn_id,
            utterance=heard,
            device_ref=device_ref,
            timeout_ms=self._interpretation_timeout_ms,
        )
        understanding_started = time.monotonic()
        try:
            proposal = await self._understand(request, context=previous)
        except InterpretationError as exc:
            if is_current():
                context.clear()
            _log.warning("smarthome understanding turn=%s unavailable code=%s", turn_id, exc.code)
            return card("unavailable", "家居指令理解服务暂不可用，请稍后重试")
        finally:
            _log.info("home stage turn=%s stage=understanding elapsed_ms=%.1f", turn_id,
                      (time.monotonic() - understanding_started) * 1000)
        if not is_current():
            return card("unavailable", "本轮已被新的输入替代或会话已结束")
        if isinstance(proposal, HomeCancellation):
            context.clear()
            return card("answered", "已取消，本次没有执行设备操作")
        if isinstance(proposal, HomeClarification):
            # Both LLM tools carry semantic facts. Presentation is selected here,
            # never by parsing device names from model-written question text.
            if proposal.action is not None and len(proposal.targets) >= 2:
                try:
                    current = await self._directory.snapshot(owner_id)
                except SmartHomeUnavailable:
                    return card("unavailable", UNAVAILABLE)
                if not is_current():
                    return card("unavailable", "本轮已被新的输入替代或会话已结束")
                selection = Proposal(intent="control", target_status="ambiguous",
                                     targets=proposal.targets, action=proposal.action)
                result = await self._ambiguous(card, owner_id, device_ref, current.registry,
                                               selection, clarification=proposal.question)
                if result.outcome == "ambiguous":
                    context.remember(heard, selection, question=result.message)
                    return result
            # Retain an unfinished request across a further clarification, but
            # never turn the conversation into an unbounded chat history.
            unfinished = (
                f"{previous['previous_utterance']}；补充：{heard}"[:MAX_UTTERANCE]
                if previous and previous['pending'] else heard
            )
            context.remember(unfinished, None, question=proposal.question, clarification=proposal)
            return card("clarification", proposal.question)
        context.clear()
        if proposal is None:
            return card("failed", NOT_UNDERSTOOD)
        if proposal.intent == "unrelated":
            return card("unrelated", UNRELATED)
        if proposal.target_status == "none":
            return card("not_found", not_found(proposal.mention))

        # The proposal was made against the snapshot above; act on what is true now.
        try:
            home = await self._directory.snapshot(owner_id)
        except SmartHomeUnavailable:
            return card("unavailable", UNAVAILABLE)
        if any(find_target(home.registry, ref) is None for ref in proposal.targets):
            return card("not_found", NO_SUCH_DEVICE)
        if not is_current():
            return card("unavailable", "本轮已被新的输入替代或会话已结束")
        if proposal.intent == "query":
            result = self._answer(card, home, proposal)
            if result.outcome == "answered":
                context.remember(heard, proposal)
            return result
        assert proposal.action is not None  # a control proposal always carries one
        if proposal.target_status == "ambiguous":
            result = await self._ambiguous(card, owner_id, device_ref, home.registry, proposal)
            if result.outcome == "ambiguous":
                context.remember(heard, proposal, question=result.message)
            # _ambiguous may execute the sole capable device. Do not remember
            # the original ambiguous proposal as a confirmed target.
            return result
        try:
            plan = plan_action(home.registry, proposal.targets, proposal.action)
        except SmartHomeError:
            return card("failed", NOT_UNDERSTOOD)
        if not is_current():
            return card("unavailable", "本轮已被新的输入替代或会话已结束")
        result = await self._run(card, owner_id, device_ref, plan)
        if is_current() and result.outcome == "executed":
            context.remember(heard, proposal)
        return result

    async def _understand(self, request: InterpretationRequest, *, context: dict | None = None) -> HomeUnderstanding:
        independent: Proposal | None = None
        if self._fallback is not None and (context is not None or self._independent_interpreter is not None):
            if self._independent_interpreter is not None:
                # Require explicit grounding without the speaker's room filling
                # an omitted target. No history is deleted to manufacture this.
                witness_request = request.model_copy(update={"origin": Origin()})
                witness = await self._independent_interpreter.interpret(witness_request)
                independent = _accepted(witness_request, witness)
                if independent is not None and (
                    independent.intent == "unrelated" or independent.target_status != "resolved"
                ):
                    independent = None
            if independent is None:
                _log.info("home interpretation turn=%s route=llm reason=%s", request.interpretation_id,
                          "context_required" if context is not None else "semantic_required")
                return await self._fallback_proposal(request, context=context)
        proposal: Proposal | None = None
        try:
            result = await self._interpreter.interpret(request)
        except InterpretationError as exc:
            # A failed interpreter is "not understood yet", never "unrelated".
            _log.info("smarthome interpretation %s failed: %s", request.interpretation_id, exc)
            if self._fallback is None:
                raise
        else:
            if self._confident(result):
                proposal = _accepted(request, result)
                if independent is not None and proposal != independent:
                    _log.info("home interpretation turn=%s reason=independent_disagreement", request.interpretation_id)
                    proposal = None
        if proposal is not None or self._fallback is None:
            _log.info("home interpretation turn=%s route=primary proposal=%s", request.interpretation_id,
                      proposal.model_dump_json() if proposal else None)
            return proposal
        _log.info("home interpretation turn=%s route=llm reason=primary_not_accepted", request.interpretation_id)
        return await self._fallback_proposal(request, context=context)

    async def _fallback_proposal(self, request: InterpretationRequest, *, context: dict | None = None) -> HomeUnderstanding:
        assert self._fallback is not None
        try:
            async with asyncio.timeout(self._fallback_timeout_s):
                suggested = await self._fallback.propose(request, context=context)
        except TimeoutError as exc:
            raise InterpretationError(ERROR_TIMEOUT, "home fallback deadline") from exc
        except InterpretationError as exc:
            _log.info("smarthome fallback %s failed: %r", request.interpretation_id, exc)
            raise
        if isinstance(suggested, HomeClarification):
            known = {candidate.ref for candidate in request.candidates}
            if len(set(suggested.targets)) != len(suggested.targets) or any(
                ref not in known for ref in suggested.targets
            ):
                raise InterpretationError(ERROR_INVALID_PROPOSAL, "clarification_outside_request")
            if suggested.action is not None and len(suggested.targets) >= 2:
                selection = Proposal(intent="control", target_status="ambiguous",
                                     targets=suggested.targets, action=suggested.action)
                if _accepted(request, InterpretationResult(
                    interpretation_id=request.interpretation_id, status="decided",
                    proposal=selection, policy_version="fallback", model_version="fallback",
                )) is None:
                    raise InterpretationError(ERROR_INVALID_PROPOSAL, "clarification_invalid_action")
            return suggested
        if not isinstance(suggested, Proposal):
            return suggested
        accepted = _accepted(
            request,
            InterpretationResult(
                interpretation_id=request.interpretation_id,
                status="decided",
                proposal=suggested,
                policy_version="fallback",
                model_version="fallback",
            ),
        )
        if accepted is None:
            raise InterpretationError(ERROR_INVALID_PROPOSAL, "fallback_outside_request")
        return accepted

    def _confident(self, result: InterpretationResult) -> bool:
        if self._min_confidence == 0 or result.proposal is None:
            return True
        keys = ["intent_p"]
        if result.proposal.intent != "unrelated":
            keys.append("device_p")
            if result.proposal.intent == "control":
                keys.append("action_p")
        return all(
            isinstance(result.diagnostics.get(key), int | float)
            and not isinstance(result.diagnostics[key], bool)
            and result.diagnostics[key] >= self._min_confidence
            for key in keys
        )

    def _answer(self, card: _Card, home: HomeSnapshot, proposal: Proposal) -> VoiceResult:
        devices = [d for d in map(home.registry.device, proposal.targets) if d is not None]
        if not devices:
            return card("failed", NOT_UNDERSTOOD)
        lines = [describe(d, home.status.get(d.device_id)) for d in devices]
        return card("answered", clip("；".join(lines)))

    async def _ambiguous(
        self,
        card: _Card,
        owner_id: str,
        device_ref: str | None,
        registry: Registry,
        proposal: Proposal,
        *, clarification: str | None = None,
    ) -> VoiceResult:
        assert proposal.action is not None
        capable: list[tuple[Device, Command]] = []
        refused: list[tuple[Device, str]] = []
        for device in (registry.device(ref) for ref in proposal.targets):
            if device is None:
                continue  # a scene cannot be a tap-to-choose candidate
            try:
                capable.append((device, device_command(device, proposal.action)))
            except SmartHomeError as exc:
                refused.append((device, exc.code))
        if clarification is not None and len(capable) < 2:
            return card("clarification", clarification)
        if not capable:
            if refused:
                device, code = refused[0]
                return card("failed", failure(device.name, code, device.type))
            return card("failed", NOT_UNDERSTOOD)
        if len(capable) == 1:  # the only one that can do it
            action = proposal.action
            plan = plan_devices([capable[0][0]], lambda _d: action)
            return await self._run(card, owner_id, device_ref, plan)
        if len(capable) > MAX_CANDIDATES:
            return card("failed", "符合的设备太多，请说出具体名称")
        # One action for every candidate, so the command is the same whichever is tapped.
        command = capable[0][1]
        return VoiceResult(
            turn_id=card.turn_id,
            utterance=card.utterance,
            outcome="ambiguous",
            message=choose_one([d.name for d, _c in capable]),
            candidates=tuple(
                PanelCandidate(device_id=d.device_id, name=d.name) for d, _c in capable
            ),
            command=CommandTemplate(
                trait=command.trait, command=command.command, params=command.params
            ),
        )

    async def _run(
        self, card: _Card, owner_id: str, device_ref: str | None, plan: CommandPlan
    ) -> VoiceResult:
        started = time.monotonic()
        try:
            execution = await self._actuator.execute(
                owner_id,
                plan,
                request_id=request_id("voice", card.turn_id),
                origin=ExecuteOrigin(kind="voice", device_ref=device_ref, turn_id=card.turn_id),
            )
        except SmartHomeUnavailable:
            return card("unavailable", UNAVAILABLE)
        finally:
            _log.info("home stage turn=%s stage=execution elapsed_ms=%.1f", card.turn_id,
                      (time.monotonic() - started) * 1000)
        completion, message = summarize(plan, execution)
        return card(_OUTCOMES[completion], message)


_OUTCOMES: dict[str, VoiceOutcome] = {
    "complete": "executed",
    "partial": "partial",
    "none": "failed",
}


class _Card:
    """Builds the non-ambiguous VoiceResults of one turn."""

    def __init__(self, turn_id: str, utterance: str) -> None:
        self.turn_id = turn_id
        self.utterance = utterance

    def __call__(self, outcome: VoiceOutcome, message: str) -> VoiceResult:
        return VoiceResult(
            turn_id=self.turn_id, utterance=self.utterance, outcome=outcome, message=message
        )


def _accepted(request: InterpretationRequest, result: InterpretationResult) -> Proposal | None:
    if result.status != "decided" or result.proposal is None:
        return None
    try:
        validate_proposal(request, result)
    except ValueError as exc:
        _log.warning("smarthome proposal %s rejected: %s", request.interpretation_id, exc)
        return None
    return result.proposal
