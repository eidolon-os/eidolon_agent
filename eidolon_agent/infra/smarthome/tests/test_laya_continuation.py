import asyncio
import json

import httpx
import pytest
from eidolon_sdk.biz.interpretation import Action, InterpretationError, Proposal
from eidolon_sdk.biz.smarthome.samples import apartment

from eidolon_agent.domain.smarthome.command import interpretation_request
from eidolon_agent.domain.smarthome.context import HomeCancellation, HomeContext
from eidolon_agent.infra.interpretation import LayaInterpreter
from eidolon_agent.infra.smarthome.laya_continuation import MODEL_REVISION, LayaHomeContinuation


def request(text="把它关掉"):
    return interpretation_request(
        apartment(), interpretation_id="turn", utterance=text, device_ref=None, timeout_ms=100
    )


def context(*, pending=False, targets=("living.ac",)):
    c = HomeContext()
    c.remember(
        "打开空调",
        Proposal(
            intent="control",
            target_status="ambiguous" if pending else "resolved",
            targets=targets,
            action=Action(trait="on_off", command="on"),
        ),
        question="客厅空调、主卧空调，要哪一个？" if pending else None,
        pending_action="打开" if pending else "",
        response="" if pending else "已打开客厅空调",
    )
    return c.snapshot()


def response(choice, probability=0.99, *, revision=MODEL_REVISION, key="follow"):
    return {
        "revision": revision,
        "truncated": [],
        "answers": {
            key: {
                "type": "choice",
                "choice": choice,
                "probabilities": {choice: probability},
                "confidence": 1.0,
            }
        },
    }


async def invoke(payload, *, ctx=None, text="把它关掉"):
    bodies = []

    def handle(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=payload)

    laya = LayaInterpreter("http://test", transport=httpx.MockTransport(handle))
    try:
        result = await LayaHomeContinuation(laya, revision=MODEL_REVISION).propose(
            request(text), context=context() if ctx is None else ctx
        )
        return result, bodies
    finally:
        await laya.aclose()


async def test_follow_uses_actual_reply_and_existing_lexicon():
    got, bodies = await invoke(response("关闭或停止"))
    assert got.targets == ("living.ac",) and got.action.command == "off"
    assert bodies[0]["state"]["context"] == {
        "上一句": "打开空调",
        "Agent": "已打开客厅空调",
        "设备": ["客厅空调"],
    }
    assert "取消" not in bodies[0]["questions"]["follow"]["criteria"]
    assert "上锁" not in bodies[0]["questions"]["follow"]["criteria"]


@pytest.mark.parametrize("probability", [0.9499, True, 1.01, -0.1])
async def test_threshold_uses_selected_probability_not_confidence(probability):
    assert (await invoke(response("关闭或停止", probability)))[0] is None


@pytest.mark.parametrize(
    "payload",
    [
        response("关闭或停止", revision="45f3dedb"),
        {**response("关闭或停止"), "truncated": ["state"]},
        response("重新理解"),
        response("取消"),
        response("上锁"),
        {"revision": MODEL_REVISION, "answers": []},
        {
            "revision": MODEL_REVISION,
            "answers": {"follow": {"type": "choice", "choice": "关闭或停止", "confidence": 1}},
        },
    ],
)
async def test_drift_and_exits_abstain(payload):
    assert (await invoke(payload))[0] is None


async def test_pick_retains_exact_action_and_order():
    ctx = context(pending=True, targets=("living.ac", "master.ac"))
    got, bodies = await invoke(response("主卧空调", key="pick"), ctx=ctx, text="后面那个")
    assert (
        got.targets == ("master.ac",)
        and got.action == Proposal.model_validate(ctx["proposal"]).action
    )
    assert list(bodies[0]["questions"]["pick"]["criteria"]) == [
        "客厅空调",
        "主卧空调",
        "取消",
        "重新理解",
    ]


@pytest.mark.parametrize("probability,accepted", [(0.5, True), (0.4999, False)])
async def test_cancel_only_pending_and_calibrated(probability, accepted):
    got, _ = await invoke(
        response("取消", probability, key="pick"),
        ctx=context(pending=True, targets=("living.ac", "master.ac")),
    )
    assert isinstance(got, HomeCancellation) == accepted


@pytest.mark.parametrize(
    "change",
    [
        {"question": "主卧空调、客厅空调，要哪一个？"},
        {"pending_action": ""},
        {"proposal": None},
        {"response": ""},
    ],
)
async def test_ineligible_context_never_calls_model(change):
    ctx = context(
        pending="question" in change or "pending_action" in change,
        targets=("living.ac", "master.ac"),
    )
    ctx.update(change)
    got, bodies = await invoke(response("关闭或停止"), ctx=ctx)
    assert got is None and not bodies


async def test_removed_and_lock_focus_never_call_model():
    for targets in [("gone",), tuple(d.device_id for d in apartment().devices if d.type == "lock")]:
        assert targets
        got, bodies = await invoke(response("打开或启动"), ctx=context(targets=targets))
        assert got is None and not bodies


async def test_missing_set_value_abstains_but_relative_amount_is_preserved():
    assert (await invoke(response("设为指定的数值或模式"), text="调一下"))[0] is None
    got, _ = await invoke(response("调高或增大"), text="再高两度")
    assert got.action.command == "step" and got.action.slots[0].value == 2


@pytest.mark.parametrize("choice,probability,text,reason,outcome", [
    ("重新理解", 0.9651, "讲个故事", "reinterpret", "abstained"),
    ("关闭或停止", 0.90, "关了它", "low_probability", "abstained"),
    ("设为指定的数值或模式", 0.99, "调一下", "action_mapping_failed", "abstained"),
    ("关闭或停止", 0.99, "关了它", "follow_action", "proposed"),
])
async def test_decision_log_distinguishes_exit_confidence_and_mapping(
    caplog, choice, probability, text, reason, outcome,
):
    with caplog.at_level("INFO", logger="eidolon_agent.infra.smarthome.laya_continuation"):
        await invoke(response(choice, probability), text=text)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    for part in (f"choice={choice!r}", f"probability={probability:.4f}",
                 f"reason={reason}", f"outcome={outcome}", "turn=turn", "question=follow"):
        assert part in message
    assert text not in message


async def test_timeout_and_cancel_propagate_without_closing_borrowed_transport():
    async def slow(req):
        await asyncio.sleep(1)
        return httpx.Response(200, json=response("关闭或停止"))

    laya = LayaInterpreter("http://test", transport=httpx.MockTransport(slow))
    adapter = LayaHomeContinuation(laya, revision=MODEL_REVISION)
    try:
        with pytest.raises(InterpretationError):
            await adapter.propose(request(), context=context())
        task = asyncio.create_task(adapter.propose(request(), context=context()))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not laya._client.is_closed
    finally:
        await laya.aclose()


@pytest.mark.parametrize('text', [
    '再关闭电视', '关掉音箱', '关闭电视', '电视关掉', '关闭音箱', '音箱关掉',
    '把音箱关掉', '别关电视', '不是电视，打开音箱', '主卧的也打开',
    '只关主卧灯，客厅的别动', '打开地下室的除湿机', '开启观影模式',
])
async def test_explicit_references_never_inherit_old_follow_target(text, caplog):
    with caplog.at_level('INFO', logger='eidolon_agent.infra.smarthome.laya_continuation'):
        got, bodies = await invoke(response('关闭或停止', .9999), text=text)
    assert got is None and not bodies
    assert 'reason=explicit_reference' in caplog.text


@pytest.mark.parametrize('text', ['把它关掉', '把它们关掉', '全关掉', '再暗一点'])
async def test_multiple_focus_requires_full_scope_interpretation(text, caplog):
    with caplog.at_level('INFO', logger='eidolon_agent.infra.smarthome.laya_continuation'):
        got, bodies = await invoke(response('关闭或停止', .9999),
                                   ctx=context(targets=('living.main_light', 'master.light')), text=text)
    assert got is None and not bodies
    assert 'reason=multiple_focus' in caplog.text


async def test_exact_name_selects_existing_pending_action_without_model_guessing():
    ctx = context(pending=True, targets=('living.tv', 'living.speaker'))
    ctx['question'] = '电视、智能音箱，要哪一个？'
    got, bodies = await invoke(response('重新理解', key='pick'), ctx=ctx, text='音箱')
    assert got.targets == ('living.speaker',) and got.action.command == 'on'
    assert not bodies


@pytest.mark.parametrize('text', ['音箱先别动', '不是音箱', '音箱也取消', '音箱打开'])
async def test_exact_pick_never_discards_other_words(text):
    ctx = context(pending=True, targets=('living.tv', 'living.speaker'))
    ctx['question'] = '电视、智能音箱，要哪一个？'
    got, bodies = await invoke(response('重新理解', key='pick'), ctx=ctx, text=text)
    assert got is None and len(bodies) == 1


async def test_exact_name_outside_pending_candidates_is_reinterpreted():
    ctx = context(pending=True, targets=('living.ac', 'master.ac'))
    got, bodies = await invoke(response('客厅空调', key='pick'), ctx=ctx, text='音箱')
    assert got is None and not bodies
