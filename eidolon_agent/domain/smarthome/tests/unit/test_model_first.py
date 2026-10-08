import asyncio
import pytest
from eidolon_sdk.biz.interpretation import Action, Proposal, Slot
from eidolon_agent.domain.smarthome.context import HomeContext, HomeCancellation
from eidolon_agent.domain.smarthome.tests.conftest import OWNER
from .test_command import _command, _Fixed, _Fallback


async def test_three_turn_history_is_bounded_and_does_not_alias(directory, executor):
    context = HomeContext()
    primary = _Fixed(
        Proposal(
            intent="control",
            target_status="resolved",
            targets=("living.main_light",),
            action=Action(trait="on_off", command="on"),
        )
    )
    command = _command(directory, executor, interpreter=primary)
    for n in range(5):
        await command.handle(OWNER, None, str(n), f"打开客厅灯{n}", context=context)
    assert [h["utterance"] for h in primary.requests[-1].context["history"]] == [
        "打开客厅灯1",
        "打开客厅灯2",
        "打开客厅灯3",
    ]
    snap = context.snapshot()
    snap["history"][0]["utterance"] = "tampered"
    assert context.snapshot()["history"][0]["utterance"] == "打开客厅灯2"
    assert primary.requests[-1].context["history"][0]["utterance"] == "打开客厅灯1"


@pytest.mark.parametrize("size", [1, 3, 5])
def test_history_window_setting(size):
    c = HomeContext(history_limit=size)
    for n in range(8):
        c.remember(str(n), None, response="回答")
    assert len(c.snapshot()["history"]) == size
    c.clear()
    assert c.snapshot() is None and c.history == []


async def test_relative_humidifier_repeats_use_latest_state(directory, executor):
    context = HomeContext()
    primary = _Fixed(
        Proposal(
            intent="control",
            target_status="resolved",
            targets=("master.humidifier",),
            action=Action(trait="fan_speed", command="step", slots=(Slot(name="delta", value=10),)),
        )
    )
    command = _command(directory, executor, interpreter=primary)
    observed = []
    for i, delta in enumerate([10, 10, 10, -10]):
        primary.proposal = primary.proposal.model_copy(
            update={
                "action": Action(
                    trait="fan_speed", command="step", slots=(Slot(name="delta", value=delta),)
                )
            }
        )
        result = await command.handle(
            OWNER,
            None,
            f"speed-{i}",
            "加湿器调大一点儿" if delta > 0 else "加湿器调小",
            context=context,
        )
        observed.append(directory.status["master.humidifier"].state["speed"])
        assert str(observed[-1]) in result.message and result.outcome == "executed"
    assert observed == [40, 50, 60, 50]
    assert [c[3] for c in executor.commands] == [
        {"delta": 10},
        {"delta": 10},
        {"delta": 10},
        {"delta": -10},
    ]


async def test_cancel_after_execution_does_not_claim_undo(directory, executor):
    context = HomeContext()
    await _command(directory, executor).handle(OWNER, None, "on", "打开客厅灯", context=context)
    result = await _command(
        directory, executor, interpreter=_Fixed(), fallback=_Fallback(HomeCancellation())
    ).handle(OWNER, None, "cancel", "不开客厅的", context=context)
    assert "未撤销上次操作" in result.message
    assert len(executor.requests) == 1 and directory.status["living.main_light"].state["on"]
    assert context.snapshot()["outcome"] == "cancelled" and context.proposal is None


async def test_handoff_carries_untrusted_proposal_scores_and_reason(directory, executor):
    proposal = Proposal(
        intent="control",
        target_status="resolved",
        targets=("living.main_light",),
        action=Action(trait="on_off", command="off"),
    )
    primary = _Fixed(proposal, diagnostics={"intent_p": 0.99, "device_p": 0.6, "action_p": 0.99})
    fallback = _Fallback(HomeCancellation())
    await _command(
        directory, executor, interpreter=primary, fallback=fallback, min_confidence=0.8
    ).handle(OWNER, None, "h", "别关灯")
    h = fallback.contexts[0]["laya_handoff"]
    assert h["proposal"]["targets"] == ["living.main_light"] and h["reason"] == "low_confidence"
    assert h["diagnostics"]["device_p"] == 0.6 and not executor.requests


@pytest.mark.parametrize("score,allowed", [(0.98, False), (0.99, True), (float("inf"), False)])
async def test_unvalidated_model_policy_has_explicit_conservative_floor(
    directory, executor, score, allowed
):
    from eidolon_sdk.biz.interpretation import InterpretationResult

    c = _command(directory, executor, min_confidence=0.8)
    p = Proposal(
        intent="control",
        target_status="resolved",
        targets=("living.main_light",),
        action=Action(trait="on_off", command="on"),
    )
    r = InterpretationResult(
        interpretation_id="threshold",
        status="decided",
        proposal=p,
        policy_version="laya-smarthome-context-v2",
        model_version="fixed",
        diagnostics={"intent_p": score, "device_p": score, "action_p": score},
    )
    assert c._confident(r) is allowed


async def test_fallback_expiry_cannot_execute_old_context(directory, executor):
    context = HomeContext()
    await _command(directory, executor).handle(OWNER, None, "first", "打开客厅灯", context=context)
    context.expires_at = __import__("time").monotonic() + 0.01

    class Slow(_Fallback):
        async def propose(self, request, *, context=None):
            await asyncio.sleep(0.02)
            return Proposal(
                intent="control",
                target_status="resolved",
                targets=("living.main_light",),
                action=Action(trait="on_off", command="off"),
            )

    r = await _command(directory, executor, interpreter=_Fixed(), fallback=Slow()).handle(
        OWNER, None, "late", "关了它", context=context
    )
    assert r.outcome == "clarification" and len(executor.requests) == 1
