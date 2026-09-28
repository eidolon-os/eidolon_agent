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
from eidolon_sdk.biz.smarthome import Command, Registry, Scene, VoiceResult

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

    async def propose(self, request: InterpretationRequest, *, context=None) -> Proposal | None:
        self.calls += 1
        self.contexts.append(context)
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


async def test_request_carries_candidates_areas_and_origin(directory, executor) -> None:
    interpreter = _Fixed(None)

    await _say(
        _command(directory, executor, interpreter=interpreter, interpretation_timeout_ms=300),
        "打开空调",
    )

    request = interpreter.requests[0]
    assert request.interpretation_id == "turn-1"
    assert request.origin.device_ref == "panel-living"
    assert request.origin.area_id == "living"
    assert request.timeout_ms == 300
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


async def test_new_topic_clears_context_and_outside_candidate_never_executes(directory, executor):
    context = HomeContext()
    context.remember("关闭灯", None, question="哪盏？")
    fallback = _Fallback(Proposal(intent="unrelated", target_status="none"))
    command = _command(directory, executor, interpreter=_Fixed(), fallback=fallback)
    assert (await command.handle(OWNER, "p", "t1", "讲个笑话", context=context)).outcome == "unrelated"
    assert context.snapshot() is None
    fallback.proposal = Proposal(intent="control", target_status="resolved", targets=("foreign.device",),
                                action=Action(trait="on_off", command="on"))
    result = await command.handle(OWNER, "p", "t2", "打开灯", context=context)
    assert result.outcome == "unavailable"
    assert not executor.requests
