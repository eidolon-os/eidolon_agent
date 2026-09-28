"""The rules interpreter against the SDK sample apartment (the laya evaluation home).

Requests are built by the production builder, so these cases see exactly what
SmartHomeCommand sends. Panels are placed: ``living`` (客厅), ``master`` (主卧),
``kitchen`` (厨房); ``None`` is a speaker with no placement.
"""

from __future__ import annotations

import pytest
from eidolon_sdk.biz.interpretation import (
    ERROR_INVALID_REQUEST,
    InterpretationError,
    InterpretationRequest,
)
from eidolon_sdk.biz.smarthome import Placement, Registry
from eidolon_sdk.biz.smarthome.samples import apartment

from eidolon_agent.domain.smarthome import interpretation_request
from eidolon_agent.infra.interpretation import RulesInterpreter

pytestmark = pytest.mark.unit

_ROOMS = ("living", "master", "kitchen")
_HOME = Registry.model_validate(
    {
        **apartment().model_dump(),
        "placements": [Placement(device_ref=f"panel-{a}", area_id=a).model_dump() for a in _ROOMS],
    }
)
_LIGHTS = ("living.main_light", "master.light", "master.bedside")
_ACS = ("living.ac", "master.ac")


def _request(text: str, room: str | None = None, **overrides) -> InterpretationRequest:
    request = interpretation_request(
        _HOME,
        interpretation_id="turn-1",
        utterance=text,
        device_ref=f"panel-{room}" if room else None,
        timeout_ms=500,
    )
    return request.model_copy(update=overrides) if overrides else request


def _control(status, targets, trait, command, **params):
    return ("control", status, tuple(targets), f"{trait}.{command}", params, None)


def _query(status, targets=(), mention=None):
    return ("query", status, tuple(targets), None, {}, mention)


def _missing(mention, action, **params):
    return ("control", "none", (), action, params, mention)


UNRELATED = ("unrelated", "none", (), None, {}, None)

# (utterance, speaker's room, expected); expected is a reading or "abstain:<reason>".
CASES = [
    # device name / alias first
    ("打开客厅空调", None, _control("resolved", ["living.ac"], "on_off", "on")),
    ("把客厅空调调到二十六度", None, _control("resolved", ["living.ac"], "thermostat", "set_target", celsius=26)),
    ("主卧空调调到25.5度", None, _control("resolved", ["master.ac"], "thermostat", "set_target", celsius=25.5)),
    ("打开主卧灯", None, _control("resolved", ["master.light"], "on_off", "on")),
    ("帮我把台灯打开", None, _control("resolved", ["master.bedside"], "on_off", "on")),
    ("打开大灯", None, _control("resolved", ["living.main_light"], "on_off", "on")),
    ("客厅主灯调到40%", None, _control("resolved", ["living.main_light"], "level", "set", value=40)),
    ("客厅主灯亮度四十", None, _control("resolved", ["living.main_light"], "level", "set", value=40)),
    ("主卧空调调高两度", None, _control("resolved", ["master.ac"], "thermostat", "step", delta=2)),
    ("电视声音大一点", None, _control("resolved", ["living.tv"], "volume", "step", delta=10)),
    ("电视音量调到30", None, _control("resolved", ["living.tv"], "volume", "set", value=30)),
    ("电视静音", None, _control("resolved", ["living.tv"], "volume", "mute", muted=True)),
    ("打开加湿器", None, _control("resolved", ["master.humidifier"], "on_off", "on")),
    ("净化器调到百分之五十", None, _control("resolved", ["living.purifier"], "fan_speed", "set", value=50)),
    ("关闭摄像头", None, _control("resolved", ["entry.camera"], "on_off", "off")),
    ("热水器调到50度", None, _control("resolved", ["bath.water_heater"], "thermostat", "set_target", celsius=50)),
    # verbs bound per device type
    ("把窗帘拉开", None, _control("resolved", ["living.curtain"], "position", "open")),
    ("窗帘拉上", None, _control("resolved", ["living.curtain"], "position", "close")),
    ("窗帘开到一半", None, _control("resolved", ["living.curtain"], "position", "set", value=50)),
    ("窗帘打开百分之三十", None, _control("resolved", ["living.curtain"], "position", "set", value=30)),
    ("窗帘停一下", None, _control("resolved", ["living.curtain"], "position", "stop")),
    ("把洗衣机打开", None, _control("resolved", ["balcony.washer"], "operational", "start")),
    ("关掉电饭煲", None, _control("resolved", ["kitchen.rice_cooker"], "operational", "stop")),
    ("暂停洗衣机", None, _control("resolved", ["balcony.washer"], "operational", "pause")),
    ("开始扫地", None, _control("resolved", ["whole.vacuum"], "operational", "start")),
    ("扫地机器人回充", None, _control("resolved", ["whole.vacuum"], "operational", "dock")),
    ("锁门", None, _control("resolved", ["entry.lock"], "lock", "lock")),
    ("把门锁上", None, _control("resolved", ["entry.lock"], "lock", "lock")),
    ("开门", None, _control("resolved", ["entry.lock"], "lock", "unlock")),
    # the speaker's room is the default
    ("打开空调", "living", _control("resolved", ["living.ac"], "on_off", "on")),
    ("打开空调", "master", _control("resolved", ["master.ac"], "on_off", "on")),
    ("打开空调", "kitchen", _control("ambiguous", _ACS, "on_off", "on")),
    ("打开空调", None, _control("ambiguous", _ACS, "on_off", "on")),
    ("开灯", "living", _control("resolved", ["living.main_light"], "on_off", "on")),
    ("开灯", "master", _control("ambiguous", _LIGHTS[1:], "on_off", "on")),
    ("开灯", None, _control("ambiguous", _LIGHTS, "on_off", "on")),
    ("空调调到26度", "living", _control("resolved", ["living.ac"], "thermostat", "set_target", celsius=26)),
    ("空调温度调低", "master", _control("resolved", ["master.ac"], "thermostat", "step", delta=-1)),
    ("灯调暗一点", "living", _control("resolved", ["living.main_light"], "level", "step", delta=-10)),
    ("亮一点", "living", _control("resolved", ["living.main_light"], "level", "step", delta=10)),
    ("升温", "master", _control("resolved", ["master.ac"], "thermostat", "step", delta=1)),
    ("空调制冷模式", "living", _control("resolved", ["living.ac"], "thermostat", "set_mode", mode="cool")),
    ("能帮我打开空调吗", "living", _control("resolved", ["living.ac"], "on_off", "on")),
    ("打开热水器", "kitchen", _control("resolved", ["bath.water_heater"], "on_off", "on")),
    # area + device word, and quantifiers
    ("打开主卧的灯", "living", _control("ambiguous", _LIGHTS[1:], "on_off", "on")),
    ("把客厅调亮一点", "master", _control("resolved", ["living.main_light"], "level", "step", delta=10)),
    ("关掉所有灯", None, _control("resolved", _LIGHTS, "on_off", "off")),
    ("把灯都关了", "living", _control("resolved", _LIGHTS, "on_off", "off")),
    ("关掉全屋的灯", None, _control("resolved", _LIGHTS, "on_off", "off")),
    ("打开客厅所有的灯", None, _control("resolved", ["living.main_light"], "on_off", "on")),
    # scenes
    ("回家模式", None, _control("resolved", ["scene.home"], "scene", "activate")),
    ("打开观影模式", None, _control("resolved", ["scene.movie"], "scene", "activate")),
    ("启动睡眠场景", None, _control("resolved", ["scene.sleep"], "scene", "activate")),
    ("我回家了", None, UNRELATED),
    # devices this home does not have
    ("打开投影仪", None, _missing("投影仪", "on_off.on")),
    ("打开车库门", None, _missing("车库门", "on_off.on")),
    ("冰箱温度调到4度", None, _missing("冰箱", "thermostat.set_target", celsius=4)),
    ("打开厨房的灯", None, _missing("厨房的灯", "on_off.on")),
    # queries
    ("空调开着吗", "living", _query("resolved", ["living.ac"])),
    ("空调开着吗", None, _query("ambiguous", _ACS)),
    ("电视开着吗", None, _query("resolved", ["living.tv"])),
    ("门锁了吗", None, _query("resolved", ["entry.lock"])),
    ("主卧空调现在几度", None, _query("resolved", ["master.ac"])),
    ("打开空调了吗", "master", _query("resolved", ["master.ac"])),
    ("现在温度多少", None, _query("resolved", ["living.thermo"])),
    ("客厅多少度", None, _query("resolved", ["living.thermo"])),
    ("冰箱开着吗", None, _query("none", mention="冰箱")),
    # not about the home
    ("讲个笑话", None, UNRELATED),
    ("你好", "living", UNRELATED),
    ("今天天气怎么样", None, UNRELATED),
    ("今天多少度", None, UNRELATED),
    ("今天好开心", None, UNRELATED),
    ("我要开会了", None, UNRELATED),
    ("电视剧真好看", None, UNRELATED),
    ("别担心", None, UNRELATED),
    # left to the LLM fallback
    ("有点热", "living", "abstain:implicit"),
    ("太暗了", "living", "abstain:implicit"),
    ("别开空调", "living", "abstain:negation"),
    ("不要关灯", "living", "abstain:negation"),
    ("不开空调", "living", "abstain:negation"),
    ("打开空调然后关掉电视", None, "abstain:multiple_commands"),
    ("打开空调和电视", "living", "abstain:multiple_targets"),
    ("打开卧室的灯", "living", "abstain:unknown_area"),
    ("打开客厅和主卧的灯", None, "abstain:multiple_areas"),
    ("打开2号灯", "living", "abstain:multiple_values"),
    ("关闭观影模式", None, "abstain:unsupported_command"),
    ("客厅主灯色温调到4000", None, "abstain:unsupported_command"),
    ("把电视和音箱关了", None, "abstain:multiple_targets"),
    ("空调", "living", "abstain:no_command"),
    ("打开", "living", "abstain:no_target"),
    ("调到26度", "living", "abstain:no_target"),
    ("关掉所有设备", None, "abstain:no_target"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("text", "room", "expected"), CASES, ids=[c[0] + (f"@{c[1]}" if c[1] else "") for c in CASES]
)
async def test_rules_reading(text: str, room: str | None, expected) -> None:
    result = await RulesInterpreter().interpret(_request(text, room))

    assert result.interpretation_id == "turn-1"
    assert (result.policy_version, result.model_version) == ("rules-zh-v1", "rules-zh-v1")
    if isinstance(expected, str):
        assert result.status == "abstained"
        assert result.diagnostics == {"reason": expected.removeprefix("abstain:")}
        return
    assert result.status == "decided"
    proposal = result.proposal
    action = proposal.action
    got = (
        proposal.intent,
        proposal.target_status,
        proposal.targets,
        f"{action.trait}.{action.command}" if action else None,
        {slot.name: slot.value for slot in action.slots} if action else {},
        proposal.mention,
    )
    assert got == expected


def test_case_table_is_broad() -> None:
    abstains = [c for c in CASES if isinstance(c[2], str)]
    assert len(CASES) >= 40
    assert len(abstains) >= 10
    assert {c[1] for c in CASES} >= {None, *_ROOMS}


async def test_slots_carry_unit_and_raw_span() -> None:
    result = await RulesInterpreter().interpret(_request("把客厅空调调到二十六度"))
    (slot,) = result.proposal.action.slots
    assert (slot.name, slot.value, slot.unit, slot.raw_span) == ("celsius", 26, "°C", "二十六度")

    result = await RulesInterpreter().interpret(_request("客厅主灯调到40%"))
    (slot,) = result.proposal.action.slots
    assert (slot.unit, slot.raw_span) == ("%", "40%")


async def test_disallowed_intent_abstains() -> None:
    request = _request("空调开着吗", "living", allowed_intents=("control",))

    result = await RulesInterpreter().interpret(request)

    assert (result.status, result.diagnostics) == ("abstained", {"reason": "intent_not_allowed"})


async def test_other_domains_are_refused() -> None:
    request = _request("打开空调").model_copy(update={"domain": "calendar"})

    with pytest.raises(InterpretationError) as raised:
        await RulesInterpreter().interpret(request)

    assert raised.value.code == ERROR_INVALID_REQUEST


async def test_owner_names_beat_generic_words() -> None:
    home = Registry.model_validate(
        {
            "revision": 1,
            "areas": [{"area_id": "study", "name": "书房"}],
            "devices": [
                {"device_id": "study.strip", "name": "灯带", "type": "switch", "area_id": "study"},
                {
                    "device_id": "study.fridge",
                    "name": "小冰箱",
                    "aliases": ["冰箱"],
                    "type": "appliance",
                },
            ],
        }
    )

    def request(text: str) -> InterpretationRequest:
        return interpretation_request(
            home, interpretation_id="t", utterance=text, device_ref=None, timeout_ms=500
        )

    fridge = await RulesInterpreter().interpret(request("打开冰箱"))
    assert fridge.proposal.targets == ("study.fridge",)
    strip = await RulesInterpreter().interpret(request("打开书房的灯"))
    assert (strip.proposal.targets, strip.proposal.action.trait) == (("study.strip",), "on_off")
