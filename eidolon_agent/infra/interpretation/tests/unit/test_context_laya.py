import json
import httpx
import pytest
from eidolon_sdk.biz.interpretation import Action, Proposal
from eidolon_sdk.biz.smarthome.samples import apartment
from eidolon_agent.domain.smarthome.command import interpretation_request
from eidolon_agent.domain.smarthome.context import HomeContext
from eidolon_agent.infra.interpretation.adapters.laya import LayaInterpreter


def answer(choice, probability=0.995):
    return {"choice": choice, "probabilities": {choice: probability}}


async def run(context, answers, *, feature=True, truncated=None):
    bodies = []

    def response(r):
        bodies.append(json.loads(r.content))
        return httpx.Response(
            200,
            json={
                "model": "laya",
                "revision": "test",
                "features": {"question_state": feature},
                "answers": answers,
                "truncated": truncated or [],
            },
        )

    a = LayaInterpreter("http://model", transport=httpx.MockTransport(response))
    request = interpretation_request(
        apartment(), interpretation_id="t", utterance="床头的", device_ref=None, timeout_ms=1000
    ).model_copy(update={"context": context})
    try:
        return await a.interpret(request), bodies
    finally:
        await a.aclose()


def pending():
    c = HomeContext()
    c.remember(
        "关灯",
        Proposal(
            intent="control",
            target_status="ambiguous",
            targets=("living.main_light", "master.bedside"),
            action=Action(trait="on_off", command="off"),
        ),
        question="客厅主灯、床头灯，要哪一个？",
        pending_action="关闭",
    )
    return c.snapshot()


@pytest.mark.parametrize("feature,cut", [(False, []), (True, ["context_device"]), (True, ["pick"])])
async def test_unsupported_or_truncated_context_never_executes(feature, cut):
    r, _ = await run(
        pending(),
        {
            "intent": answer("控制"),
            "device": answer("床头灯"),
            "action": answer("关闭或停止"),
            "pick": answer("床头灯"),
        },
        feature=feature,
        truncated=cut,
    )
    assert r.status == "abstained" and r.proposal is None


async def test_pending_action_is_used_only_with_model_agreement():
    r, bodies = await run(
        pending(),
        {
            "intent": answer("控制", 0.7),
            "device": answer("床头灯"),
            "action": answer("打开或启动", 0.8),
            "pick": answer("床头灯", 0.98),
        },
    )
    assert (
        len(bodies) == 1
        and r.proposal.targets == ("master.bedside",)
        and r.proposal.action.command == "off"
    )
    assert "context" not in bodies[0]["state"]
    assert "最近对话" in bodies[0]["questions"]["context_device"]["state"]["context"]
    assert "最近对话" not in bodies[0]["questions"]["pick"]["state"]["context"]


async def test_conflicting_complete_command_and_pending_pick_escalates():
    r, _ = await run(
        pending(),
        {
            "intent": answer("控制"),
            "device": answer("床头灯"),
            "action": answer("打开或启动"),
            "pick": answer("床头灯", 0.98),
        },
    )
    assert r.status == "abstained" and r.diagnostics["reason"] == "model_disagreement"


async def test_uncertain_pick_does_not_execute_stateless_guessed_action():
    r, _ = await run(
        pending(),
        {
            "intent": answer("控制", 0.98),
            "device": answer("床头灯", 0.98),
            "action": answer("打开或启动", 0.94),
            "pick": answer("床头灯", 0.94),
        },
    )
    assert r.proposal is None and r.diagnostics["reason"] == "pending_not_resolved"


async def test_confident_full_command_can_replace_pending_with_model_agreement():
    result, _ = await run(pending(), {
        'intent': answer('控制'), 'device': answer('床头灯'),
        'action': answer('打开或启动'), 'pick': answer('重新理解', .98),
    })
    assert result.proposal.targets == ('master.bedside',)
    assert result.proposal.action.command == 'on'


@pytest.mark.parametrize('pick', [answer('重新理解', .94), answer('取消', .98)])
async def test_pending_replacement_does_not_override_uncertainty_or_cancellation(pick):
    result, _ = await run(pending(), {
        'intent': answer('控制'), 'device': answer('床头灯'),
        'action': answer('打开或启动'), 'pick': pick,
    })
    assert result.proposal is None
