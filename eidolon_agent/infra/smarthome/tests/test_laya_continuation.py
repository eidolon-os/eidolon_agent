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
