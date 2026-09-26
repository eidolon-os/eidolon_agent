from __future__ import annotations

import json

import httpx
import pytest
from eidolon_sdk.biz.interpretation import (
    ERROR_CONTEXT_TOO_LARGE,
    ERROR_INVALID_PROPOSAL,
    ERROR_INVALID_REQUEST,
    ERROR_TIMEOUT,
    ERROR_UNAVAILABLE,
    InterpretationError,
)
from eidolon_sdk.biz.smarthome import Placement, Registry
from eidolon_sdk.biz.smarthome.samples import apartment

from eidolon_agent.domain.smarthome import interpretation_request
from eidolon_agent.infra.interpretation import LayaInterpreter
from eidolon_agent.infra.interpretation.adapters.laya import (
    ACTION_QUESTION,
    INTENT_QUESTION,
    MULTIPLE,
    NO_DEVICE,
)

pytestmark = pytest.mark.unit

_HOME = Registry.model_validate(
    {
        **apartment().model_dump(),
        "placements": [Placement(device_ref="panel", area_id="living").model_dump()],
    }
)


def _request(text: str):
    return interpretation_request(
        _HOME, interpretation_id="turn-1", utterance=text, device_ref="panel", timeout_ms=400
    )


def _reply(intent: str, device: str, action: str = "打开或启动", **extra) -> dict:
    def answer(choice: str, p: float = 0.9) -> dict:
        return {"type": "choice", "choice": choice, "probabilities": {choice: p}, "confidence": 0.8}

    return {
        "model": "laya-multilingual",
        "answers": {
            "intent": answer(intent),
            "device": answer(device, 0.7),
            "action": answer(action),
        },
        "usage": {"input_tokens": 300, "output_tokens": 0},
        "truncated": [],
        "backend": "onnx",
        "timing_ms": {"forward": 60.0, "total": 75.5},
        **extra,
    }


def _laya(handler, **kwargs) -> tuple[LayaInterpreter, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return LayaInterpreter(
        "http://laya.test:8771/", transport=httpx.MockTransport(record), **kwargs
    ), seen


def _json(body: dict, status: int = 200):
    return lambda _request: httpx.Response(status, json=body)


async def test_asks_the_scenarios_three_questions_word_for_word() -> None:
    laya, seen = _laya(_json(_reply("控制", "客厅空调")), api_key="k")

    await laya.interpret(_request("打开空调"))

    (sent,) = seen
    assert (sent.method, str(sent.url)) == ("POST", "http://laya.test:8771/v1/systemone")
    assert sent.headers["authorization"] == "Bearer k"
    body = json.loads(sent.content)
    assert body["state"] == {"utterance": "打开空调"}
    assert body["questions"]["intent"] == INTENT_QUESTION
    assert body["questions"]["action"] == ACTION_QUESTION
    device = body["questions"]["device"]
    assert device["instructions"] == "`utterance` 说的是家里的哪台设备？"
    criteria = device["criteria"]
    assert criteria["客厅空调"] == "客厅·空调"
    assert criteria["空气净化器"] == "客厅·风扇/净化/加湿"
    assert list(criteria)[-2:] == [MULTIPLE, NO_DEVICE]  # exits after the devices
    assert len(criteria) == 18 + 2  # scenes are the 整屋 exit, not options
    await laya.aclose()


async def test_control_answer_becomes_a_proposal() -> None:
    laya, _ = _laya(_json(_reply("控制", "客厅空调")))

    result = await laya.interpret(_request("打开空调"))

    assert result.status == "decided"
    assert result.model_version == "laya-multilingual"
    assert result.policy_version == "laya-smarthome-v1"
    proposal = result.proposal
    assert (proposal.intent, proposal.target_status, proposal.targets) == (
        "control",
        "resolved",
        ("living.ac",),
    )
    assert (proposal.action.trait, proposal.action.command) == ("on_off", "on")
    assert result.diagnostics == {
        "intent_p": 0.9,
        "device_p": 0.7,
        "action_p": 0.9,
        "backend": "onnx",
        "server_ms": 75.5,
    }


async def test_numbers_come_from_the_shared_lexicon() -> None:
    laya, _ = _laya(_json(_reply("控制", "客厅空调", "设为指定的数值或模式")))

    result = await laya.interpret(_request("空调调到二十六度"))

    action = result.proposal.action
    assert (action.trait, action.command) == ("thermostat", "set_target")
    assert [(s.name, s.value, s.raw_span) for s in action.slots] == [("celsius", 26, "二十六度")]


@pytest.mark.parametrize(
    ("reply", "text", "expected"),
    [
        (_reply("无关", NO_DEVICE), "讲个笑话", ("unrelated", "none", ())),
        (_reply("查询", "电视"), "电视开着吗", ("query", "resolved", ("living.tv",))),
        (_reply("查询", NO_DEVICE), "冰箱开着吗", ("query", "none", ())),
        (_reply("控制", NO_DEVICE), "打开投影仪", ("control", "none", ())),
    ],
)
async def test_answer_mapping(reply, text, expected) -> None:
    laya, _ = _laya(_json(reply))

    proposal = (await laya.interpret(_request(text))).proposal

    assert (proposal.intent, proposal.target_status, proposal.targets) == expected


@pytest.mark.parametrize(
    ("reply", "text", "reason"),
    [
        (_reply("控制", MULTIPLE), "回家模式", "multiple_devices"),
        (_reply("控制", "客厅主灯", "上锁"), "锁上客厅主灯", "unsupported_command"),
        (_reply("控制", "客厅空调", "设为指定的数值或模式"), "空调调一下", "unsupported_command"),
    ],
)
async def test_what_laya_cannot_name_is_abstained(reply, text, reason) -> None:
    laya, _ = _laya(_json(reply))

    result = await laya.interpret(_request(text))

    assert result.status == "abstained"
    assert result.diagnostics["reason"] == reason


@pytest.mark.parametrize(
    "body",
    [
        {"answers": {}},
        {"no": "answers"},
        _reply("控制", "不存在的设备"),
        _reply("maybe", "客厅空调"),
        {"answers": {"intent": "控制", "device": {}, "action": {}}},
    ],
)
async def test_malformed_replies_are_invalid_proposals(body) -> None:
    laya, _ = _laya(_json(body))

    with pytest.raises(InterpretationError) as raised:
        await laya.interpret(_request("打开空调"))

    assert raised.value.code == ERROR_INVALID_PROPOSAL


async def test_non_json_reply_is_an_invalid_proposal() -> None:
    laya, _ = _laya(lambda _r: httpx.Response(200, text="<html>"))

    with pytest.raises(InterpretationError) as raised:
        await laya.interpret(_request("打开空调"))

    assert raised.value.code == ERROR_INVALID_PROPOSAL


def _raise(exc: Exception):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "code", "retryable"),
    [
        (_raise(httpx.ReadTimeout("slow")), ERROR_TIMEOUT, True),
        (_raise(httpx.ConnectError("refused")), ERROR_UNAVAILABLE, True),
        (_json({"error": {"code": "busy", "message": "x"}}, 503), ERROR_UNAVAILABLE, True),
        (_json({"error": {"code": "unauthorized", "message": "x"}}, 401), ERROR_UNAVAILABLE, True),
        (
            _json({"error": {"code": "invalid_question", "message": "x"}}, 400),
            ERROR_INVALID_REQUEST,
            False,
        ),
        (lambda _r: httpx.Response(413, text="too big"), ERROR_CONTEXT_TOO_LARGE, False),
    ],
)
async def test_transport_failures_map_to_interpretation_errors(handler, code, retryable) -> None:
    laya, _ = _laya(handler)

    with pytest.raises(InterpretationError) as raised:
        await laya.interpret(_request("打开空调"))

    assert (raised.value.code, raised.value.retryable) == (code, retryable)


async def test_duplicate_device_names_get_distinct_options() -> None:
    home = Registry.model_validate(
        {
            "revision": 1,
            "areas": [{"area_id": "a", "name": "客厅"}, {"area_id": "b", "name": "主卧"}],
            "devices": [
                {"device_id": "a.light", "name": "灯", "type": "light", "area_id": "a"},
                {"device_id": "b.light", "name": "灯", "type": "light", "area_id": "b"},
            ],
        }
    )
    laya, seen = _laya(_json(_reply("控制", "灯（主卧）")))
    request = interpretation_request(
        home, interpretation_id="t", utterance="打开主卧的灯", device_ref=None, timeout_ms=400
    )

    result = await laya.interpret(request)

    assert list(json.loads(seen[0].content)["questions"]["device"]["criteria"])[:2] == [
        "灯",
        "灯（主卧）",
    ]
    assert result.proposal.targets == ("b.light",)
