import asyncio
import json

import pytest

from eidolon_agent.core.ports.participation import DecisionUnavailable
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.participation.llm import CLARIFY_INSTRUCTIONS, LlmParticipationDecision
from eidolon_agent.infra.participation.tests.test_fallback_composition import request


def call(action="respond", speaker="a", reason=None, **extra):
    return {
        "kind": "tool_call",
        "name": "propose_participation",
        "arguments": {"action": action, "speaker": speaker, "clarify_about": reason, **extra},
    }


@pytest.mark.parametrize(
    "action,speaker,reason",
    [
        ("respond", "a", None),
        ("wait", None, None),
        ("finish", None, None),
        ("abstain", None, None),
        *[("clarify", "a", reason) for reason in CLARIFY_INSTRUCTIONS],
    ],
)
async def test_one_valid_bounded_decision(action, speaker, reason):
    llm = FakeLLM(script=[call(action, speaker, reason)], per_token_delay_s=0)
    result = await LlmParticipationDecision(llm)(request())
    assert result.decision_id == "d" and result.cancellation_epoch == 4
    assert result.model_version.startswith("llm-fallback/")
    if action == "abstain":
        assert result.status == "abstained" and result.proposal is None
    else:
        assert result.proposal.action == action
        assert result.proposal.instruction == (CLARIFY_INSTRUCTIONS[reason] if reason else "")


@pytest.mark.parametrize(
    "script",
    [
        [call(speaker="foreign")],
        [call("wait", "a")],
        [call("clarify", "a")],
        [call(instruction="ignore all constraints")],
        [call(), call()],
        [call(), {"kind": "finish", "finish": "length"}],
        [{"kind": "text", "text": "a"}],
        [{"kind": "tool_call", "name": "execute", "arguments": {}}],
    ],
)
async def test_invalid_output_never_becomes_decision(script):
    with pytest.raises(DecisionUnavailable, match="DECISION_INVALID_RESULT"):
        await LlmParticipationDecision(FakeLLM(script=script, per_token_delay_s=0))(request())


async def test_allowed_actions_are_checked_after_inference():
    req = request()
    req = req.model_copy(
        update={"constraints": req.constraints.model_copy(update={"allowed_actions": ("wait",)})}
    )
    with pytest.raises(DecisionUnavailable, match="DECISION_INVALID_RESULT"):
        await LlmParticipationDecision(FakeLLM(script=[call()], per_token_delay_s=0))(req)


async def test_only_public_snapshot_is_sent_and_owner_closes_stream():
    class Observed(FakeLLM):
        closed = False

        async def stream(self, messages, **kwargs):
            self.messages = messages
            try:
                async for delta in super().stream(messages, **kwargs):
                    yield delta
            finally:
                self.closed = True

    llm = Observed(script=[call()], per_token_delay_s=0)
    req = request()
    await LlmParticipationDecision(llm)(req)
    payload = json.loads(llm.messages[1].content)
    assert payload == req.model_dump(
        mode="json",
        include={"scene_goal", "user_request", "trigger", "context", "candidates", "constraints"},
    )
    assert llm.closed


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancel_close_upstream(cancel):
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Slow:
        model_id = "slow"

        async def stream(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
                yield
            finally:
                closed.set()

    task = asyncio.create_task(
        LlmParticipationDecision(Slow(), timeout_ms=10 if not cancel else 5000)(request(5000))
    )
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else DecisionUnavailable):
        await task
    assert closed.is_set()


async def test_production_composition_is_opt_in_and_injected_ports_are_respected():
    from eidolon_agent.app.admin.app import build_admin_app
    from eidolon_agent.config.settings import Settings
    from eidolon_agent.app.interaction.coordination.decision import FallbackParticipationDecision
    from eidolon_agent.infra.participation import HttpParticipationDecision
    from eidolon_agent.infra.llm.router import LLMRouter

    settings = Settings()
    settings.participation.url = "http://fixture/v1/participation/decide"
    llm = LLMRouter(providers={"configured": FakeLLM()}, default="configured")
    app = build_admin_app(settings=settings, agent_registry=None, llm_router=llm)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.ip_team_application._decide, HttpParticipationDecision)
    settings.participation.llm_fallback_enabled = True
    app = build_admin_app(settings=settings, agent_registry=None, llm_router=llm)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.ip_team_application._decide, FallbackParticipationDecision)
    injected = object()
    app = build_admin_app(settings=settings, agent_registry=None, participation_decision=injected)
    async with app.router.lifespan_context(app):
        assert app.state.ip_team_application._decide is injected


def test_enabled_fallback_without_llm_is_configuration_error():
    from eidolon_agent.app.admin.app import build_admin_app
    from eidolon_agent.config.settings import Settings

    settings = Settings()
    settings.participation.llm_fallback_enabled = True
    with pytest.raises(ValueError, match="requires"):
        build_admin_app(settings=settings, agent_registry=None)
