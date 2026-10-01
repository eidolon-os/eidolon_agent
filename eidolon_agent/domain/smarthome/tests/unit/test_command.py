from __future__ import annotations

import time

import pytest
from eidolon_sdk.biz.interpretation import (
    ERROR_UNAVAILABLE,
    Action,
    InterpretationError,
    InterpretationRequest,
    InterpretationResult,
    Proposal,
    Slot,
)
from eidolon_sdk.biz.smarthome import Command, HomeSessionScope, Registry, Scene, VoiceResult

from eidolon_agent.domain.smarthome import DeviceStatus, SmartHomeCommand
from eidolon_agent.domain.smarthome.context import HomeCancellation, HomeClarification, HomeContext
from eidolon_agent.domain.smarthome.tests.conftest import OWNER
from eidolon_agent.infra.interpretation import RulesInterpreter

pytestmark = pytest.mark.unit


def _command(directory, executor, *, interpreter=None, fallback=None, **kwargs) -> SmartHomeCommand:
    return SmartHomeCommand(
        directory=directory,
        executor=executor,
        interpreter=interpreter or RulesInterpreter(),
        fallback=fallback,
        **kwargs,
    )


async def _say(command: SmartHomeCommand, text: str, device: str | None = "panel-living"):
    return await command.handle(OWNER, device, "turn-1", text)


class _Fixed:
    """An interpreter that always answers with the given proposal (or raises)."""

    def __init__(
        self, proposal: Proposal | None = None, error: Exception | None = None,
        diagnostics: dict | None = None,
    ) -> None:
        self.proposal = proposal
        self.error = error
        self.diagnostics = diagnostics or {}
        self.requests: list[InterpretationRequest] = []

    async def interpret(self, request: InterpretationRequest) -> InterpretationResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return InterpretationResult(
            interpretation_id=request.interpretation_id,
            status="decided" if self.proposal else "abstained",
            proposal=self.proposal,
            policy_version="test",
            model_version="test",
            diagnostics=self.diagnostics,
        )


class _Fallback:
    def __init__(self, proposal: Proposal | None = None, error: Exception | None = None) -> None:
        self.proposal = proposal
        self.error = error
        self.calls = 0
        self.contexts = []
        self.requests = []

    async def propose(self, request: InterpretationRequest, *, context=None) -> Proposal | None:
        self.calls += 1
        self.contexts.append(context)
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.proposal


async def test_executes_resolved_command_in_the_speakers_room(directory, executor) -> None:
    before_ms = time.time() * 1000
    result = await _say(_command(directory, executor), "打开空调")
    after_ms = time.time() * 1000

    assert result == VoiceResult(
        turn_id="turn-1", utterance="打开空调", outcome="executed", message="已打开客厅空调"
    )
    request = executor.requests[0]
    assert request.request_id == "voice:turn-1"
    assert before_ms + 3000 - 1 <= request.deadline_ms <= after_ms + 3000  # absolute epoch ms
    assert request.scene_id is None
    assert request.origin.kind == "voice"
    assert request.origin.device_ref == "panel-living"
    assert request.origin.turn_id == "turn-1"
    assert executor.commands == [("living.ac", "on_off", "on", {})]
    assert directory.status["living.ac"].state["on"] is True


async def test_same_words_from_another_room_address_that_room(directory, executor) -> None:
    result = await _say(_command(directory, executor), "打开空调", device="box3-master")

    assert result.message == "已打开主卧空调"
    assert executor.commands == [("master.ac", "on_off", "on", {})]


async def test_unplaced_device_gets_candidates_and_a_template(directory, executor) -> None:
    result = await _say(_command(directory, executor), "打开空调", device="unplaced-panel")

    assert result.outcome == "ambiguous"
    assert [(c.device_id, c.name) for c in result.candidates] == [
        ("living.ac", "客厅空调"),
        ("master.ac", "主卧空调"),
    ]
    assert result.command is not None
    assert (result.command.trait, result.command.command, result.command.params) == (
        "on_off",
        "on",
        {},
    )
    assert result.message == "客厅空调、主卧空调，要哪一个？"
    assert executor.requests == []


@pytest.mark.parametrize(
    ("text", "message", "command"),
    [
        (
            "客厅主灯调到40%",
            "客厅主灯 已调到 40%",
            ("living.main_light", "level", "set", {"value": 40}),
        ),
        (
            "空调调到二十四度",
            "客厅空调 已设为 24°C",
            ("living.ac", "thermostat", "set_target", {"celsius": 24}),
        ),
        (
            "灯调亮一点",
            "客厅主灯 已调到 70%",
            ("living.main_light", "level", "step", {"delta": 10}),
        ),
        ("电视静音", "电视 已静音", ("living.tv", "volume", "mute", {"muted": True})),
        ("把门锁上", "智能门锁 已上锁", ("entry.lock", "lock", "lock", {})),
        ("扫地机回充", "扫地机器人 正在回充", ("whole.vacuum", "operational", "dock", {})),
        ("窗帘开一半", "客厅窗帘 已调到 50%", ("living.curtain", "position", "set", {"value": 50})),
    ],
)
async def test_messages_say_what_the_provider_confirmed(
    directory, executor, text, message, command
) -> None:
    result = await _say(_command(directory, executor), text)

    assert (result.outcome, result.message) == ("executed", message)
    assert executor.commands == [command]


async def test_all_lights_report_partial_success_honestly(directory, executor) -> None:
    executor.offline.add("master.bedside")

    result = await _say(_command(directory, executor), "关掉所有灯")

    assert result.outcome == "partial"
    assert result.message == "已关闭客厅主灯、主卧灯；床头灯 离线，没有执行"


async def test_scene_is_sent_by_id_for_the_runtime_to_expand(directory, executor) -> None:
    result = await _say(_command(directory, executor), "回家模式")

    assert (result.outcome, result.message) == ("executed", "已执行回家模式")
    (request,) = executor.requests
    assert (request.scene_id, request.commands) == ("scene.home", ())
    assert directory.status["living.main_light"].state == {"on": True, "level": 70}


async def test_scene_partial_success_is_counted(directory, executor) -> None:
    executor.offline.add("living.ac")

    result = await _say(_command(directory, executor), "回家模式")

    assert result.outcome == "partial"
    assert result.message == "回家模式：完成 2 项，客厅空调 离线，没有执行"


async def test_scene_report_follows_what_the_runtime_ran(directory, executor) -> None:
    # The Runtime expands the scene as stored at execution time, not our snapshot.
    executor.scenes["scene.home"] = Scene(
        scene_id="scene.home",
        name="回家",
        actions=(Command(device_id="living.tv", trait="on_off", command="on"),),
    )
    executor.offline.add("living.tv")

    result = await _say(_command(directory, executor), "回家模式")

    assert (result.outcome, result.message) == ("failed", "回家模式：电视 离线，没有执行")


@pytest.mark.parametrize(
    ("text", "deadline_ms", "message"),
    [
        ("回家模式", 3000, "回家模式 已不存在，没有执行"),
        ("打开空调", 0, "客厅空调 超时，没有执行"),
    ],
)
async def test_request_refused_before_running_is_definite(
    directory, executor, text, deadline_ms, message
) -> None:
    executor.scenes.clear()

    result = await _say(_command(directory, executor, execute_deadline_ms=deadline_ms), text)

    assert (result.outcome, result.message) == ("failed", message)


async def test_unconfirmed_command_never_claims_success(directory, executor) -> None:
    executor.unknown.add("living.ac")

    result = await _say(_command(directory, executor), "打开空调")

    assert result.outcome == "failed"
    assert result.message == "没收到确认，客厅空调可能没有执行"


async def test_missing_reply_by_the_deadline_is_unknown(directory, executor) -> None:
    executor.delay_s = 1.0
    command = _command(directory, executor, execute_deadline_ms=10)

    result = await _say(command, "打开空调")

    assert result.outcome == "failed"
    assert result.message.startswith("没收到确认")


async def test_out_of_range_value_is_refused_before_executing(directory, executor) -> None:
    result = await _say(_command(directory, executor), "空调调到35度")

    assert (result.outcome, result.message) == ("failed", "客厅空调 只能设在 16–30°C")
    assert executor.requests == []


@pytest.mark.parametrize(
    ("text", "outcome", "message"),
    [
        ("打开投影仪", "not_found", "家里没有投影仪"),
        ("冰箱开着吗", "not_found", "家里没有冰箱"),
        ("打开厨房的灯", "not_found", "家里没有厨房的灯"),
        ("讲个笑话", "unrelated", "这里只处理家里的设备"),
        ("有点热", "failed", "没听明白，换个说法试试"),
        ("别开空调", "failed", "没听明白，换个说法试试"),
        ("   ", "failed", "没听明白，换个说法试试"),
    ],
)
async def test_non_executions(directory, executor, text, outcome, message) -> None:
    result = await _say(_command(directory, executor), text)

    assert (result.outcome, result.message) == (outcome, message)
    assert executor.requests == []


async def test_query_answers_from_directory_state(directory, executor) -> None:
    directory.status["living.ac"] = DeviceStatus(
        online=True, state={"on": True, "mode": "cool", "target_c": 26, "current_c": 28.5}
    )

    result = await _say(_command(directory, executor), "客厅空调开着吗")

    assert (result.outcome, result.message) == ("answered", "客厅空调 开着，制冷 26°C，室温 28.5°C")
    assert executor.requests == []


async def test_ambiguous_query_answers_every_candidate(directory, executor) -> None:
    directory.status["master.ac"] = DeviceStatus(
        online=False, state={"on": False, "mode": "cool", "target_c": 26, "current_c": None}
    )

    result = await _say(_command(directory, executor), "空调开着吗", device=None)

    assert result.outcome == "answered"
    assert result.message == "客厅空调 关着；主卧空调 离线（上次：关着）"


async def test_sensor_query(directory, executor) -> None:
    directory.status["living.thermo"] = DeviceStatus(
        online=True, state={"temp_c": 24.5, "humidity": 48}
    )

    result = await _say(_command(directory, executor), "现在温度多少")

    assert result.message == "温湿度计 24.5°C，湿度 48%"


async def test_unreachable_runtime_is_unavailable(directory, executor) -> None:
    directory.unavailable = True
    assert (await _say(_command(directory, executor), "打开空调")).outcome == "unavailable"

    directory.unavailable = False
    executor.unavailable = True
    result = await _say(_command(directory, executor), "打开空调")
    assert (result.outcome, result.message) == ("unavailable", "暂时连不上家居服务")


async def test_proposal_is_rechecked_against_the_current_registry(directory, executor) -> None:
    gone = Registry.model_validate(
        {
            **directory.registries[0].model_dump(),
            "revision": 2,
            "devices": [
                d.model_dump()
                for d in directory.registries[0].devices
                if d.device_id != "living.ac"
            ],
            "scenes": [],
        }
    )
    directory.registries.append(gone)

    result = await _say(_command(directory, executor), "打开空调")

    assert (result.outcome, result.message) == ("not_found", "家里没有这个设备")
    assert executor.requests == []


async def test_abstained_goes_to_the_fallback(directory, executor) -> None:
    fallback = _Fallback(
        Proposal(
            intent="control",
            target_status="resolved",
            targets=("living.ac",),
            action=Action(
                trait="thermostat", command="step", slots=(Slot(name="delta", value=-2),)
            ),
        )
    )

    result = await _say(_command(directory, executor, fallback=fallback), "有点热")

    assert fallback.calls == 1
    assert (result.outcome, result.message) == ("executed", "客厅空调 已调到 24°C")


async def test_low_confidence_home_proposal_uses_fallback_before_execution(directory, executor) -> None:
    proposed = Proposal(
        intent="control", target_status="resolved", targets=("living.tv",),
        action=Action(trait="on_off", command="on"),
    )
    fallback = _Fallback(proposed)
    interpreter = _Fixed(
        Proposal(
            intent="control", target_status="resolved", targets=("living.ac",),
            action=Action(trait="on_off", command="on"),
        ),
        diagnostics={"intent_p": 0.96, "device_p": 0.51, "action_p": 0.94},
    )

    result = await _say(
        _command(directory, executor, interpreter=interpreter, fallback=fallback, min_confidence=0.8),
        "打开电视",
    )

    assert result.outcome == "executed"
    assert fallback.calls == 1
    assert executor.commands == [("living.tv", "on_off", "on", {})]


async def test_interpreter_failure_is_not_read_as_unrelated(directory, executor) -> None:
    interpreter = _Fixed(error=InterpretationError(ERROR_UNAVAILABLE, "down"))
    fallback = _Fallback(None)

    result = await _say(
        _command(directory, executor, interpreter=interpreter, fallback=fallback), "打开空调"
    )

    assert fallback.calls == 1
    assert (result.outcome, result.message) == ("failed", "没听明白，换个说法试试")


async def test_fallback_service_error_is_not_blame_for_user_wording(
    directory, executor
) -> None:
    outside = Proposal(
        intent="control",
        target_status="resolved",
        targets=("garage.door",),
        action=Action(trait="on_off", command="on"),
    )
    command = _command(
        directory,
        executor,
        interpreter=_Fixed(outside),
        fallback=_Fallback(error=InterpretationError(ERROR_UNAVAILABLE)),
    )

    result = await _say(command, "打开车库门")

    assert result.outcome == "unavailable"
    assert result.message == "家居指令理解服务暂不可用，请稍后重试"
    assert executor.requests == []


async def test_only_capable_candidate_of_an_ambiguity_is_executed(directory, executor) -> None:
    proposal = Proposal(
        intent="control",
        target_status="ambiguous",
        targets=("living.main_light", "living.tv"),
        action=Action(trait="level", command="step", slots=(Slot(name="delta", value=-10),)),
    )

    result = await _say(_command(directory, executor, interpreter=_Fixed(proposal)), "暗一点")

    assert result.message == "客厅主灯 已调到 50%"
    assert executor.commands == [("living.main_light", "level", "step", {"delta": -10})]


@pytest.mark.parametrize("budget", [None, 300])
async def test_request_carries_candidates_areas_and_origin(directory, executor, budget) -> None:
    interpreter = _Fixed(None)

    await _say(
        _command(directory, executor, interpreter=interpreter,
                 **({"interpretation_timeout_ms": budget} if budget is not None else {})),
        "打开空调",
    )

    request = interpreter.requests[0]
    assert request.interpretation_id == "turn-1"
    assert request.origin.device_ref == "panel-living"
    assert request.origin.area_id == "living"
    assert request.timeout_ms == (1000 if budget is None else budget)
    assert len(request.candidates) == 18 + 4
    assert {c.kind for c in request.candidates if c.ref.startswith("scene.")} == {"scene"}
    assert [a.name for a in request.areas][:2] == ["客厅", "主卧"]


async def test_long_utterance_is_clipped_for_the_card(directory, executor) -> None:
    result = await _say(_command(directory, executor), "你好" * 300)

    assert result.outcome == "unrelated"
    assert len(result.utterance) == 200


async def test_clarification_reply_resumes_pending_action_then_relative_followup(directory, executor):
    ambiguous = Proposal(intent="control", target_status="ambiguous",
                         targets=("living.ac", "master.ac"), action=Action(trait="on_off", command="off"))
    interpreter = _Fixed(ambiguous)
    fallback = _Fallback(Proposal(intent="control", target_status="resolved", targets=("living.ac",),
                                  action=Action(trait="on_off", command="off")))
    command = _command(directory, executor, interpreter=interpreter, fallback=fallback)
    context = HomeContext()
    first = await command.handle(OWNER, "unplaced", "t1", "关闭空调", context=context)
    assert first.outcome == "ambiguous"
    assert not executor.requests
    second = await command.handle(OWNER, "unplaced", "t2", "客厅那个", context=context)
    assert second.outcome == "executed"
    assert fallback.contexts[0]["pending"] is True
    assert fallback.contexts[0]["proposal"]["action"]["command"] == "off"
    assert len(interpreter.requests) == 1  # a short reply must not go to single-turn Laya
    assert executor.commands == [("living.ac", "on_off", "off", {})]
    fallback.proposal = Proposal(intent="control", target_status="resolved", targets=("living.ac",),
                                action=Action(trait="thermostat", command="step", slots=(Slot(name="delta", value=-2),)))
    third = await command.handle(OWNER, "unplaced", "t3", "再低两度", context=context)
    assert third.outcome == "executed"
    assert fallback.contexts[-1]["pending"] is False
    assert fallback.contexts[-1]["proposal"]["targets"] == ["living.ac"]


async def test_missing_action_asks_question_and_cancellation_clears_pending(directory, executor):
    fallback = _Fallback(HomeClarification("想对客厅灯做什么？"))
    command = _command(directory, executor, interpreter=_Fixed(), fallback=fallback)
    context = HomeContext()
    result = await command.handle(OWNER, "p", "t1", "客厅灯", context=context)
    assert result.outcome == "clarification"
    assert context.pending
    assert not executor.requests
    fallback.proposal = HomeCancellation()
    result = await command.handle(OWNER, "p", "t2", "算了", context=context)
    assert result.outcome == "answered"
    assert context.snapshot() is None
    assert not executor.requests


async def test_expired_or_closed_context_cannot_trigger_pending_action(directory, executor):
    context = HomeContext()
    context.remember("关闭灯", None, question="哪盏？")
    context.expires_at = 0
    fallback = _Fallback()
    command = _command(directory, executor, interpreter=_Fixed(), fallback=fallback)
    await command.handle(OWNER, "p", "t1", "那个", context=context)
    assert fallback.contexts == [None]
    context.active = False
    fallback.proposal = Proposal(intent="control", target_status="resolved", targets=("living.ac",),
                                action=Action(trait="on_off", command="off"))
    result = await command.handle(OWNER, "p", "t2", "关闭空调", context=context)
    assert result.outcome == "unavailable"
    assert not executor.requests


async def test_new_topic_clears_action_focus_and_outside_candidate_never_executes(directory, executor):
    context = HomeContext()
    context.remember("关闭灯", None, question="哪盏？")
    fallback = _Fallback(Proposal(intent="unrelated", target_status="none"))
    command = _command(directory, executor, interpreter=_Fixed(), fallback=fallback)
    assert (await command.handle(OWNER, "p", "t1", "讲个笑话", context=context)).outcome == "unrelated"
    snapshot = context.snapshot()
    assert snapshot["previous_utterance"] == "讲个笑话"
    assert snapshot["proposal"] is None and snapshot["pending"] is False
    assert snapshot["known_targets"] == [] and snapshot["known_action"] is None
    fallback.proposal = Proposal(intent="control", target_status="resolved", targets=("foreign.device",),
                                action=Action(trait="on_off", command="on"))
    result = await command.handle(OWNER, "p", "t2", "打开灯", context=context)
    assert result.outcome == "unavailable"
    assert not executor.requests


async def test_non_command_discourse_can_ground_a_later_explicit_request(directory, executor):
    context = HomeContext()
    fallback = _Fallback(Proposal(intent="unrelated", target_status="none"))
    command = _command(directory, executor, interpreter=_Fixed(), fallback=fallback)
    await command.handle(OWNER, None, "statement", "家人让我把客厅主灯关闭", context=context)
    before = context.snapshot()
    assert not executor.requests
    assert before["proposal"] is None and before["pending"] is False
    fallback.proposal = Proposal(intent="control", target_status="resolved",
                                targets=("living.main_light",),
                                action=Action(trait="on_off", command="off"))
    result = await command.handle(OWNER, None, "request", "对，我现在就是要你关掉", context=context)
    assert result.outcome == "executed"
    assert fallback.contexts[-1] == before
    assert executor.commands == [("living.main_light", "on_off", "off", {})]


async def test_repeated_ambiguity_has_same_buttons_from_either_llm_tool(directory, executor):
    action = Action(trait="on_off", command="off")
    targets = ("living.main_light", "master.light")
    fallback = _Fallback(Proposal(intent="control", target_status="ambiguous", targets=targets, action=action))
    command = _command(directory, executor, interpreter=_Fixed(), fallback=fallback)
    context = HomeContext()
    first = await command.handle(OWNER, "unplaced", "a", "关闭主灯", context=context)
    fallback.proposal = HomeClarification("客厅主灯、主卧灯，要关哪一个？", targets, action)
    second = await command.handle(OWNER, "unplaced", "b", "关闭主灯", context=context)
    assert first.outcome == second.outcome == "ambiguous"
    assert first.candidates == second.candidates
    assert first.command == second.command
    assert context.snapshot()["proposal"]["targets"] == list(targets)
    assert not executor.requests


@pytest.mark.parametrize("targets", [("foreign.device", "living.main_light"),
                                     ("living.main_light", "living.main_light")])
async def test_clarification_rejects_foreign_or_duplicate_choices(directory, executor, targets):
    fallback = _Fallback(HomeClarification("哪盏？", targets, Action(trait="on_off", command="off")))
    result = await _say(_command(directory, executor, interpreter=_Fixed(), fallback=fallback), "关闭灯")
    assert result.outcome == "unavailable"
    assert not executor.requests


async def test_missing_action_keeps_known_devices_without_executable_buttons(directory, executor):
    targets = ("living.main_light", "master.light")
    fallback = _Fallback(HomeClarification("想对灯做什么？", targets))
    context = HomeContext()
    result = await _command(directory, executor, interpreter=_Fixed(), fallback=fallback).handle(
        OWNER, "unplaced", "a", "主灯", context=context)
    assert result.outcome == "clarification"
    assert not result.candidates and result.command is None
    assert context.snapshot()["known_targets"] == list(targets)
    assert not executor.requests


async def test_question_never_executes_when_only_one_candidate_remains_capable(directory, executor):
    fallback = _Fallback(HomeClarification("调暗哪一个？", ("living.main_light", "living.tv"),
                         Action(trait="level", command="step", slots=(Slot(name="delta", value=-10),))))
    result = await _say(_command(directory, executor, interpreter=_Fixed(), fallback=fallback), "暗一点")
    assert result.outcome == "clarification"
    assert not result.candidates
    assert not executor.requests

async def test_new_input_invalidates_inflight_interpretation_before_execution(directory, executor):
    import asyncio

    from eidolon_agent.app.smarthome.application import SmartHomeApplication
    from eidolon_agent.app.smarthome.tests.test_application import authority
    scope = HomeSessionScope(owner_id=OWNER, companion_id="companion", device_ref="panel-living", session_id="s")
    entered, release = asyncio.Event(), asyncio.Event()
    class SlowRules:
        calls = 0
        async def interpret(self, request):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
            return await RulesInterpreter().interpret(request)
    interpreter = SlowRules()
    app = SmartHomeApplication(_command(directory, executor, interpreter=interpreter), interpreter=interpreter, runtime_authority=authority(OWNER, "companion"))
    first = asyncio.create_task(app.handle(scope, 'old', '打开客厅灯'))
    await entered.wait()
    second = asyncio.create_task(app.handle(scope, 'new', '关闭客厅灯'))
    await asyncio.sleep(0)
    release.set()
    old, new = await asyncio.gather(first, second)
    assert old.outcome == 'unavailable'
    assert new.outcome == 'executed'
    assert len(executor.requests) == 1 and executor.requests[0].request_id == 'voice:new'


async def test_new_input_invalidates_old_proposal_while_authority_is_pending(directory, executor):
    import asyncio

    from eidolon_agent.app.smarthome.application import SmartHomeApplication
    from eidolon_agent.app.smarthome.tests.test_application import authority

    scope = HomeSessionScope(
        owner_id=OWNER, companion_id="companion", device_ref="panel-living", session_id="s",
    )
    interpreting, release_model = asyncio.Event(), asyncio.Event()
    authorizing, release_authority = asyncio.Event(), asyncio.Event()

    class SlowRules:
        async def interpret(self, request):
            interpreting.set()
            await release_model.wait()
            return await RulesInterpreter().interpret(request)

    port = authority(OWNER, "companion")
    facts = port.resolve.return_value

    async def resolve(**_kwargs):
        if interpreting.is_set():
            authorizing.set()
            await release_authority.wait()
        return facts

    port.resolve.side_effect = resolve
    interpreter = SlowRules()
    app = SmartHomeApplication(
        _command(directory, executor, interpreter=interpreter),
        interpreter=interpreter, runtime_authority=port,
    )
    first = asyncio.create_task(app.handle(scope, "old", "打开客厅灯"))
    await interpreting.wait()
    second = asyncio.create_task(app.handle(scope, "new", "关闭客厅灯"))
    await authorizing.wait()
    release_model.set()
    assert (await asyncio.wait_for(first, 1)).outcome == "unavailable"
    assert not executor.requests
    release_authority.set()
    assert (await asyncio.wait_for(second, 1)).outcome == "executed"
    assert len(executor.requests) == 1 and executor.requests[0].request_id == "voice:new"

async def test_new_input_during_submitted_command_does_not_pretend_to_undo_it(directory, executor):
    import asyncio

    from eidolon_agent.app.smarthome.application import SmartHomeApplication
    from eidolon_agent.app.smarthome.tests.test_application import authority
    scope = HomeSessionScope(owner_id=OWNER, companion_id="companion", device_ref="panel-living", session_id="s")
    executor.delay_s = 0.05
    app = SmartHomeApplication(_command(directory, executor), interpreter=RulesInterpreter(), runtime_authority=authority(OWNER, "companion"))
    first = asyncio.create_task(app.handle(scope, 'submitted', '打开客厅灯'))
    while not executor.requests:
        await asyncio.sleep(0)
    second = asyncio.create_task(app.handle(scope, 'correction', '关闭客厅灯'))
    old,new = await asyncio.gather(first,second)
    assert old.outcome == new.outcome == 'executed'
    assert [r.request_id for r in executor.requests] == ['voice:submitted','voice:correction']


async def test_complete_command_in_conversation_uses_primary_without_llm(directory, executor):
    context = HomeContext()
    context.remember('打开窗帘', Proposal(intent='control', target_status='resolved',
                     targets=('living.curtain',), action=Action(trait='position', command='open')))
    proposal = Proposal(intent='control', target_status='resolved', targets=('living.curtain',),
                        action=Action(trait='position', command='close'))
    primary, fallback = _Fixed(proposal), _Fallback()
    command = _command(directory, executor, interpreter=primary, fallback=fallback,
                       independent_interpreter=RulesInterpreter(require_complete=True))
    result = await command.handle(OWNER, 'panel-living', 'next', '关闭窗帘。', context=context)
    assert result.outcome == 'executed'
    assert fallback.calls == 0 and len(primary.requests) == 1
    assert executor.commands == [('living.curtain', 'position', 'close', {})]
    assert context.proposal == proposal


@pytest.mark.parametrize('text', [
    '打开它', '客厅那个', '再暗一点', '算了', '等一下', '打开客厅灯，不，关掉',
    '别关主卧灯，打开客厅灯', '如果天黑就打开客厅灯', '打开客厅灯以后再关闭窗帘',
    '不要打开窗帘', '关闭窗帘也取消', '我刚才说关闭窗帘', '我没说关闭窗帘',
    '开心打开窗帘', '调暗一点', '关闭灯',
])
async def test_context_required_inputs_skip_primary_without_losing_plan(directory, executor, text):
    context = HomeContext()
    context.remember('关闭主灯', None, question='客厅还是主卧？')
    before = context.snapshot()
    primary, fallback = _Fixed(), _Fallback(HomeCancellation())
    command = _command(directory, executor, interpreter=primary, fallback=fallback,
                       independent_interpreter=RulesInterpreter(require_complete=True))
    await command.handle(OWNER, 'panel-living', 'next', text, context=context)
    assert not primary.requests and not executor.requests
    assert fallback.contexts == [before]


@pytest.mark.parametrize('primary_kind', ['disagree', 'abstain', 'error', 'low_confidence'])
async def test_primary_fallback_preserves_context_and_never_executes_wrong_suggestion(
    directory, executor, primary_kind,
):
    context = HomeContext()
    context.remember('关闭主灯', None, question='客厅还是主卧？')
    before = context.snapshot()
    correct = Proposal(intent='control', target_status='resolved', targets=('living.curtain',),
                       action=Action(trait='position', command='close'))
    primary = _Fixed(correct, diagnostics={'intent_p': .99, 'device_p': .99, 'action_p': .99})
    if primary_kind == 'disagree':
        primary.proposal = correct.model_copy(update={'action': Action(trait='position', command='open')})
    elif primary_kind == 'abstain':
        primary.proposal = None
    elif primary_kind == 'error':
        primary.error = InterpretationError(ERROR_UNAVAILABLE, 'busy')
    else:
        primary.diagnostics['action_p'] = .7
    fallback = _Fallback(correct)
    command = _command(directory, executor, interpreter=primary, fallback=fallback,
                       independent_interpreter=RulesInterpreter(require_complete=True), min_confidence=.8)
    result = await command.handle(OWNER, 'panel-living', 'next', '关闭窗帘', context=context)
    assert result.outcome == 'executed'
    assert fallback.contexts == [before]
    assert executor.commands == [('living.curtain', 'position', 'close', {})]


@pytest.mark.parametrize('text', ['打开它', '不要关闭窗帘', '算了', '如果天黑就打开客厅灯'])
async def test_missing_context_does_not_grant_a_guessed_target(directory, executor, text):
    primary = _Fixed(Proposal(intent='control', target_status='resolved', targets=('living.curtain',),
                            action=Action(trait='position', command='open')))
    fallback = _Fallback(HomeClarification('请说明要操作的设备或动作'))
    command = _command(directory, executor, interpreter=primary, fallback=fallback,
                       independent_interpreter=RulesInterpreter(require_complete=True))
    result = await _say(command, text)
    assert result.outcome == 'clarification'
    assert not primary.requests and not executor.requests
    assert fallback.contexts == [None]


@pytest.mark.parametrize('completion', ['success', 'offline', 'unknown'])
async def test_unique_capable_target_remembers_only_confirmed_execution(directory, executor, completion):
    action = Action(trait='level', command='step', slots=(Slot(name='delta', value=-10),))
    ambiguous = Proposal(intent='control', target_status='ambiguous',
                        targets=('living.main_light', 'living.tv'), action=action)
    primary = _Fixed(ambiguous)
    fallback = _Fallback(Proposal(intent='control', target_status='resolved',
                                 targets=('living.main_light',), action=action))
    context = HomeContext()
    if completion == 'offline':
        executor.offline.add('living.main_light')
    if completion == 'unknown':
        executor.unknown.add('living.main_light')
    command = _command(directory, executor, interpreter=primary, fallback=fallback)
    first = await command.handle(OWNER, 'p', 'unique', '调暗一点', context=context)
    if completion != 'success':
        assert first.outcome != 'executed' and context.snapshot() is None
        return
    assert first.outcome == 'executed'
    assert context.proposal.target_status == 'resolved'
    assert context.proposal.targets == ('living.main_light',)
    assert not context.pending
    await command.handle(OWNER, 'p', 'followup', '再暗一点', context=context)
    assert fallback.contexts[-1]['proposal']['targets'] == ['living.main_light']
    assert executor.commands == [('living.main_light', 'level', 'step', {'delta': -10})] * 2


async def test_continuation_executes_through_existing_authority_without_llm(directory, executor):
    context = HomeContext()
    base = _command(directory, executor)
    await base.handle(OWNER, None, "first", "打开客厅空调", context=context)
    assert context.snapshot()["response"] == "已打开客厅空调"
    continuation = _Fallback(
        Proposal(
            intent="control",
            target_status="resolved",
            targets=("living.ac",),
            action=Action(trait="on_off", command="off"),
        )
    )
    fallback = _Fallback()
    command = _command(
        directory,
        executor,
        continuation=continuation,
        fallback=fallback,
        independent_interpreter=RulesInterpreter(require_complete=True),
    )
    result = await command.handle(OWNER, None, "next", "把它关掉", context=context)
    assert result.outcome == "executed" and executor.commands[-1][2] == "off"
    assert continuation.calls == 1 and fallback.calls == 0
    assert continuation.requests[0].timeout_ms == 1000
    assert context.snapshot()["response"] == "已关闭客厅空调"


async def test_continuation_abstention_falls_back_once_with_same_context(directory, executor):
    context = HomeContext()
    await _command(directory, executor).handle(
        OWNER, None, "first", "打开客厅空调", context=context
    )
    before = context.snapshot()
    continuation, fallback = _Fallback(), _Fallback()
    primary = _Fixed()
    await _command(
        directory, executor, interpreter=primary, continuation=continuation, fallback=fallback
    ).handle(OWNER, None, "second", "那卧室的呢", context=context)
    assert continuation.calls == fallback.calls == 1 and not primary.requests
    assert fallback.contexts == [before]


async def test_late_continuation_cannot_execute_after_new_turn(directory, executor):
    import asyncio

    context = HomeContext()
    await _command(directory, executor).handle(
        OWNER, None, "first", "打开客厅空调", context=context
    )
    started, release = asyncio.Event(), asyncio.Event()

    class Slow:
        async def propose(self, request, *, context=None):
            started.set()
            await release.wait()
            return Proposal(
                intent="control",
                target_status="resolved",
                targets=("living.ac",),
                action=Action(trait="on_off", command="off"),
            )

    task = asyncio.create_task(
        _command(directory, executor, continuation=Slow(), fallback=_Fallback()).handle(
            OWNER, None, "next", "关闭它", context=context
        )
    )
    await started.wait()
    context.revision += 1
    release.set()
    result = await task
    assert result.outcome == "unavailable" and len(executor.requests) == 1


async def test_continuation_expiring_during_inference_is_not_executed(directory, executor):
    import asyncio

    context = HomeContext()
    await _command(directory, executor).handle(
        OWNER, None, "first", "打开客厅空调", context=context
    )
    context.expires_at = time.monotonic() + 0.005

    class Slow:
        async def propose(self, request, *, context=None):
            await asyncio.sleep(0.02)
            return Proposal(
                intent="control",
                target_status="resolved",
                targets=("living.ac",),
                action=Action(trait="on_off", command="off"),
            )

    fallback = _Fallback()
    await _command(directory, executor, continuation=Slow(), fallback=fallback).handle(
        OWNER, None, "next", "关闭它", context=context
    )
    assert len(executor.requests) == 1 and fallback.contexts == [None]


async def test_ambiguity_snapshot_keeps_the_shown_candidates_and_action(directory, executor):
    context = HomeContext()
    result = await _command(directory, executor).handle(
        OWNER, None, "first", "打开空调", context=context
    )
    assert result.outcome == "ambiguous"
    snapshot = context.snapshot()
    assert snapshot["proposal"]["targets"] == [c.device_id for c in result.candidates]
    assert snapshot["question"] == result.message and snapshot["pending_action"] == "打开"
