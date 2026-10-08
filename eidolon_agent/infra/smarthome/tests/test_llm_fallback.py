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


@pytest.mark.parametrize(("script", "reason"), [
    ([{"kind": "text", "text": "请说明设备"}], "no_tool_call"),
    ([_call(None)], "null_proposal"),
])
async def test_abstention_diagnostic_distinguishes_wire_results(script, reason, caplog):
    llm = FakeLLM(script=script, per_token_delay_s=0)
    with caplog.at_level("INFO", logger="eidolon_agent.infra.smarthome.llm_fallback"):
        assert await LlmHomeFallback(llm).propose(_request()) is None
    assert f"reason={reason}" in caplog.text
    assert "请说明设备" not in caplog.text


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


async def test_change_room_clarification_then_selection_executes_new_action_once():
    """Replay a real clarification shape through the Agent and virtual Provider."""
    from eidolon_agent.domain.smarthome import SmartHomeCommand
    from eidolon_agent.domain.smarthome.context import HomeContext
    from eidolon_agent.domain.smarthome.tests.conftest import (
        OWNER,
        FakeDirectory,
        FakeExecutor,
        home_registry,
    )
    from eidolon_agent.infra.interpretation import RulesInterpreter

    llm = FakeLLM(script=[[
        {"kind": "tool_call", "name": "ask_home_clarification", "arguments": {
            "question": "卧室里想关掉哪一盏灯？主卧灯还是床头灯？",
            "targets": ["master.light", "master.bedside"],
            "action": {"trait": "on_off", "command": "off", "slots": []},
        }},
    ], [_call({
        "intent": "control", "target_status": "resolved", "targets": ["master.bedside"],
        "action": {"trait": "on_off", "command": "off", "slots": []},
    })]], per_token_delay_s=0)
    directory = FakeDirectory(home_registry())
    executor = FakeExecutor(directory)
    command = SmartHomeCommand(directory=directory, executor=executor,
                               interpreter=RulesInterpreter(), fallback=LlmHomeFallback(llm))
    context = HomeContext()
    first = await command.handle(OWNER, None, "focus", "打开客厅主灯", context=context)
    assert first.outcome == "executed"
    second = await command.handle(OWNER, None, "change", "卧室的也关掉", context=context)
    assert second.outcome == "ambiguous"
    assert [c.device_id for c in second.candidates] == ["master.light", "master.bedside"]
    assert len(executor.commands) == 1
    pending = context.snapshot()
    assert pending["proposal"]["action"]["command"] == "off"
    third = await command.handle(OWNER, None, "select", "床头灯", context=context)
    assert third.outcome == "executed"
    assert executor.commands == [("living.main_light", "on_off", "on", {}),
                                 ("master.bedside", "on_off", "off", {})]
    assert llm.calls == 2
    assert context.snapshot()["pending"] is False


@pytest.mark.parametrize('text', ['把它关掉', '关掉它', '关掉那台', '这一盏关掉'])
@pytest.mark.parametrize('targets', [['a'], ['a', 'b']])
async def test_singular_reference_cannot_choose_or_expand_multiple_focus(text, targets):
    from eidolon_agent.domain.smarthome.context import HomeClarification
    llm = FakeLLM(script=[_call({
        'intent': 'control', 'target_status': 'resolved', 'targets': targets,
        'action': {'trait': 'on_off', 'command': 'off'},
    })], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(
        _request().model_copy(update={'utterance': text}),
        context={'pending': False, 'proposal': {'targets': ['a', 'b']}},
    )
    assert isinstance(result, HomeClarification)
    assert result.targets == ('a', 'b') and result.action.command == 'off'


@pytest.mark.parametrize('text', ['把它们关掉', '把它俩关掉', '客厅灯关掉'])
async def test_plural_or_explicit_selection_retains_model_proposal(text):
    llm = FakeLLM(script=[_call({
        'intent': 'control', 'target_status': 'resolved', 'targets': ['a', 'b'] if '它' in text else ['a'],
        'action': {'trait': 'on_off', 'command': 'off'},
    })], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(
        _request().model_copy(update={'utterance': text}),
        context={'pending': False, 'proposal': {'targets': ['a', 'b']}},
    )
    assert result.target_status == 'resolved'


@pytest.mark.parametrize(('text', 'spoken'), [('亮度设成150%', 150), ('亮度设成百分之一百二十', 120)])
@pytest.mark.parametrize('provenance', [{}, {'raw_span': '150%'}])
async def test_explicit_absolute_value_cannot_be_clamped_by_llm(text, spoken, provenance):
    from eidolon_agent.domain.smarthome.context import HomeClarification
    llm = FakeLLM(script=[_call({
        'intent': 'control', 'target_status': 'resolved', 'targets': ['a'],
        'action': {'trait': 'level', 'command': 'set', 'slots': [
            {'name': 'value', 'value': 100, **provenance},
        ]},
    })], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(_request().model_copy(update={'utterance': text}))
    assert isinstance(result, HomeClarification)
    assert result.targets == ('a',) and result.action is None


async def test_numeric_device_name_is_not_an_amount():
    llm = FakeLLM(script=[_call({
        'intent': 'control', 'target_status': 'resolved', 'targets': ['a'],
        'action': {'trait': 'level', 'command': 'set', 'slots': [{'name': 'value', 'value': 60}]},
    })], per_token_delay_s=0)
    request = _request().model_copy(update={
        'utterance': '3号灯亮度恢复原来的设置',
        'candidates': (Candidate(ref='a', name='3号灯', kind='light'),),
    })
    result = await LlmHomeFallback(llm).propose(request)
    assert result.action.slots[0].value == 60


async def test_quantity_disagreement_does_not_reinterpret_a_relative_fraction():
    from eidolon_agent.domain.smarthome.context import HomeClarification
    llm = FakeLLM(script=[_call({
        'intent': 'control', 'target_status': 'resolved', 'targets': ['a'],
        'action': {'trait': 'level', 'command': 'set', 'slots': [{'name': 'value', 'value': 25}]},
    })], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(
        _request().model_copy(update={'utterance': '亮度调到原来的一半'}),
    )
    assert isinstance(result, HomeClarification)
    assert result.action is None


@pytest.mark.parametrize('text', ['打开客厅灯并把亮度设为30', '打开客厅灯，同时关闭卧室灯'])
async def test_multiple_operations_cannot_be_collapsed_to_one_valid_action(text):
    from eidolon_agent.domain.smarthome.context import HomeClarification
    llm = FakeLLM(script=[_call({
        'intent': 'control', 'target_status': 'resolved', 'targets': ['a'],
        'action': {'trait': 'on_off', 'command': 'on'},
    })], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(_request().model_copy(update={'utterance': text}))
    assert isinstance(result, HomeClarification) and result.action is None


@pytest.mark.parametrize('text', ['把它打开', '客厅那台也打开', '主卧的那个关掉'])
async def test_missing_focus_cannot_be_replaced_by_a_guessed_device(text):
    from eidolon_sdk.biz.interpretation import Area

    from eidolon_agent.domain.smarthome.context import HomeClarification
    llm = FakeLLM(script=[_call({
        'intent': 'control', 'target_status': 'resolved', 'targets': ['a'],
        'action': {'trait': 'on_off', 'command': 'on'},
    })], per_token_delay_s=0)
    request = _request().model_copy(update={
        'utterance': text,
        'areas': (Area(area_id='living', name='客厅'), Area(area_id='master', name='主卧')),
    })
    result = await LlmHomeFallback(llm).propose(request, context=None)
    assert isinstance(result, HomeClarification)
    assert not result.targets and result.action is None


async def test_pending_known_target_is_valid_focus_without_a_complete_proposal():
    llm = FakeLLM(script=[_call({
        'intent': 'control', 'target_status': 'resolved', 'targets': ['a'],
        'action': {'trait': 'on_off', 'command': 'on'},
    })], per_token_delay_s=0)
    result = await LlmHomeFallback(llm).propose(
        _request().model_copy(update={'utterance': '把它打开'}),
        context={'pending': True, 'proposal': None, 'known_targets': ['a']},
    )
    assert result.targets == ('a',) and result.action.command == 'on'


async def test_model_handoff_and_relative_semantics_reach_llm_transport():
    import json
    captured=[]
    class Capture(FakeLLM):
        async def stream(self,messages,**kwargs):
            captured.extend(messages)
            async for delta in super().stream(messages,**kwargs):
                yield delta
    llm=Capture(script=[_call({'intent':'unrelated','target_status':'none','targets':[]})],per_token_delay_s=0)
    context={'history':[{'utterance':'开灯','response':'已打开','outcome':'executed'}],
             'laya_handoff':{'reason':'low_confidence','proposal':None,'diagnostics':{'device_p':.6}}}
    await LlmHomeFallback(llm).propose(_request(),context=context)
    body=json.loads(captured[-1].content)
    assert body['context'] == context
    assert '可补全也可推翻' in captured[0].content
    assert '禁止根据历史回复猜测绝对值set' in captured[0].content
