"""The home LLM can propose, but never calls an actuator itself."""

from __future__ import annotations

import pytest
from eidolon_sdk.biz.interpretation import Candidate, InterpretationError, InterpretationRequest

from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.smarthome.llm_fallback import LlmHomeFallback


async def test_llm_fallback_returns_one_structured_proposal() -> None:
    llm = FakeLLM(script=[{
        "kind": "tool_call", "name": "propose_home_action",
        "arguments": {"proposal": {
            "intent": "control", "target_status": "resolved", "targets": ["living.tv"],
            "action": {"trait": "on_off", "command": "on", "slots": []},
        }},
    }], per_token_delay_s=0)
    request = InterpretationRequest(
        interpretation_id="turn-1", domain="smarthome", utterance="打开电视",
        candidates=(Candidate(ref="living.tv", name="电视", kind="media"),), timeout_ms=1000,
    )

    proposal = await LlmHomeFallback(llm).propose(request)

    assert proposal is not None
    assert proposal.targets == ("living.tv",)
    assert (proposal.action.trait, proposal.action.command) == ("on_off", "on")
    assert llm.calls == 1


def _request():
    return InterpretationRequest(
        interpretation_id="fallback-regression", domain="smarthome", utterance="打开灯",
        candidates=(Candidate(ref="a", name="客厅灯", kind="light"),
                    Candidate(ref="b", name="卧室灯", kind="light")), timeout_ms=800,
    )


def _call(proposal):
    return {"kind": "tool_call", "name": "propose_home_action", "arguments": {"proposal": proposal}}


async def test_ambiguous_targets_are_preserved_for_domain_clarification():
    llm = FakeLLM(script=[_call({
        "intent": "control", "target_status": "ambiguous", "targets": ["a", "b"],
        "action": {"trait": "on_off", "command": "on"},
    })], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(_request())
    assert result.target_status == "ambiguous"
    assert result.targets == ("a", "b")


@pytest.mark.parametrize("proposal", [None, {
    "intent": "unrelated", "target_status": "none", "targets": [], "action": None,
}])
async def test_abstention_and_unrelated_are_distinct_valid_results(proposal):
    llm = FakeLLM(script=[_call(proposal)], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(_request())
    assert result is None if proposal is None else result.intent == "unrelated"


@pytest.mark.parametrize("script", [
    [_call({"intent": "control", "target_status": "resolved", "targets": ["a"]})],
    [_call({"intent": "unrelated", "target_status": "none", "targets": ["a"]})],
    [_call(None), _call(None)],
    [{"kind": "finish", "finish": "length"}],
    [{"kind": "tool_call", "name": "execute", "arguments": {}}],
])
async def test_invalid_or_incomplete_model_output_is_not_silent_abstention(script):
    llm = FakeLLM(script=script, per_token_delay_s=0)
    with pytest.raises(InterpretationError) as exc:
        await LlmHomeFallback(llm).propose(_request())
    assert exc.value.code == "INVALID_PROPOSAL"


@pytest.mark.parametrize(("name", "arguments", "kind"), [
    ("ask_home_clarification", {"question": "想调节哪个设备？", "targets": [], "action": None}, "HomeClarification"),
    ("cancel_home_command", {}, "HomeCancellation"),
])
async def test_question_and_cancel_are_distinct_non_executable_tools(name, arguments, kind):
    llm = FakeLLM(script=[{"kind": "tool_call", "name": name, "arguments": arguments}], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(_request())
    assert type(result).__name__ == kind


async def test_original_mention_contract_violation_is_reported_not_swallowed():
    llm = FakeLLM(script=[_call({
        "intent": "control", "target_status": "resolved", "targets": ["a"],
        "action": {"trait": "on_off", "command": "on", "slots": []},
        "mention": "打开客厅灯",
    })], per_token_delay_s=0)
    with pytest.raises(InterpretationError, match="invalid_tool_arguments"):
        await LlmHomeFallback(llm).propose(_request())


async def test_clarification_carries_structured_candidates_and_action():
    llm = FakeLLM(script=[{"kind": "tool_call", "name": "ask_home_clarification", "arguments": {
        "question": "客厅灯还是卧室灯？", "targets": ["a", "b"],
        "action": {"trait": "on_off", "command": "off"},
    }}], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(_request())
    assert result.targets == ("a", "b")
    assert result.action.command == "off"


async def test_question_alone_is_no_longer_a_complete_model_contract():
    llm = FakeLLM(script=[{"kind": "tool_call", "name": "ask_home_clarification",
                          "arguments": {"question": "客厅灯还是卧室灯？"}}], per_token_delay_s=0)
    with pytest.raises(InterpretationError, match="invalid_tool_arguments"):
        await LlmHomeFallback(llm).propose(_request())
