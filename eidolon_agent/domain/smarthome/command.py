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

from eidolon_sdk.biz.interpretation import (
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
    ) -> None:
        self._directory = directory
        self._interpreter = interpreter
        self._fallback = fallback
        self._interpretation_timeout_ms = interpretation_timeout_ms
        self._fallback_timeout_s = fallback_timeout_s
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0 and 1")
        self._min_confidence = min_confidence
        self._actuator = HomeActuator(executor, deadline_ms=execute_deadline_ms)

    async def handle(
        self, owner_id: str, device_ref: str | None, turn_id: str, utterance: str
    ) -> VoiceResult:
        heard = " ".join(utterance.split())
        card = _Card(turn_id, heard[:MAX_SHOWN])
        if not heard:
            return card("failed", NOT_UNDERSTOOD)
        try:
            home = await self._directory.snapshot(owner_id)
        except SmartHomeUnavailable:
            return card("unavailable", UNAVAILABLE)
        request = interpretation_request(
            home.registry,
            interpretation_id=turn_id,
            utterance=heard,
            device_ref=device_ref,
            timeout_ms=self._interpretation_timeout_ms,
        )
        proposal = await self._understand(request)
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
        if proposal.intent == "query":
            return self._answer(card, home, proposal)
        assert proposal.action is not None  # a control proposal always carries one
        if proposal.target_status == "ambiguous":
            return await self._ambiguous(card, owner_id, device_ref, home.registry, proposal)
        try:
            plan = plan_action(home.registry, proposal.targets, proposal.action)
        except SmartHomeError:
            return card("failed", NOT_UNDERSTOOD)
        return await self._run(card, owner_id, device_ref, plan)

    async def _understand(self, request: InterpretationRequest) -> Proposal | None:
        proposal: Proposal | None = None
        try:
            result = await self._interpreter.interpret(request)
        except InterpretationError as exc:
            # A failed interpreter is "not understood yet", never "unrelated".
            _log.info("smarthome interpretation %s failed: %s", request.interpretation_id, exc)
        else:
            if self._confident(result):
                proposal = _accepted(request, result)
        if proposal is not None or self._fallback is None:
            return proposal
        try:
            async with asyncio.timeout(self._fallback_timeout_s):
                suggested = await self._fallback.propose(request)
        except (InterpretationError, TimeoutError) as exc:
            _log.info("smarthome fallback %s failed: %r", request.interpretation_id, exc)
            return None
        if suggested is None:
            return None
        return _accepted(
            request,
            InterpretationResult(
                interpretation_id=request.interpretation_id,
                status="decided",
                proposal=suggested,
                policy_version="fallback",
                model_version="fallback",
            ),
        )

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
        try:
            execution = await self._actuator.execute(
                owner_id,
                plan,
                request_id=request_id("voice", card.turn_id),
                origin=ExecuteOrigin(kind="voice", device_ref=device_ref, turn_id=card.turn_id),
            )
        except SmartHomeUnavailable:
            return card("unavailable", UNAVAILABLE)
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
