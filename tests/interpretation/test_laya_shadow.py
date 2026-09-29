from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from eidolon_agent.interpretation.contracts import (
    MODEL_CONTRACT_VERSION,
    DeviceOption,
    InterpretationRequest,
)
from eidolon_agent.interpretation.laya import LayaInterpretationAdapter
from eidolon_agent.interpretation.shadow import DirectoryDevice, ShadowInterpreter


def _request() -> InterpretationRequest:
    return InterpretationRequest(
        "打开客厅灯", (DeviceOption("lamp-1", "客厅灯", "客厅", "灯"),), "turn-1", "owner-1"
    )


def _reply(*, revision: str = "45f3dedb", device: str = "客厅灯") -> dict:
    def answer(choice: str) -> dict:
        return {"type": "choice", "choice": choice, "confidence": 0.91}

    return {
        "contract_version": MODEL_CONTRACT_VERSION,
        "revision": revision,
        "truncated": [],
        "answers": {
            "intent": answer("控制"),
            "device": answer(device),
            "action": answer("打开或启动"),
        },
    }


@pytest.mark.asyncio
async def test_adapter_only_maps_whitelisted_suggestion() -> None:
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, json=_reply())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await LayaInterpretationAdapter(client=client).interpret(_request())
    assert result is not None and result.device_id == "lamp-1"
    assert result.action == "打开或启动" and result.source == "laya"
    assert set(seen[0]["questions"]["device"]["criteria"]) == {
        "客厅灯",
        "没有对应的设备",
        "多个设备或整屋",
    }
    assert "owner-1" not in json.dumps(seen[0], ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        _reply(revision="wrong"),
        _reply(device="unlisted"),
        {**_reply(), "contract_version": "v2"},
        {**_reply(), "truncated": ["device"]},
    ],
)
async def test_version_or_response_drift_abstains(reply: dict) -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=reply))
    async with httpx.AsyncClient(transport=transport) as client:
        assert await LayaInterpretationAdapter(client=client).interpret(_request()) is None


@pytest.mark.asyncio
async def test_concurrent_failures_abstain_without_commands() -> None:
    calls = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = LayaInterpretationAdapter(client=client)
        results = await asyncio.gather(*(adapter.interpret(_request()) for _ in range(24)))
    assert results == [None] * 24 and calls == 24


@pytest.mark.asyncio
async def test_deadline_abstains() -> None:
    async def handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=_reply())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = LayaInterpretationAdapter(client=client, timeout_s=0.01)
        assert await adapter.interpret(_request()) is None


class Directory:
    async def list_devices(self, **kwargs):
        assert kwargs["owner_id"] == "owner-1"
        return [
            DirectoryDevice("lamp-1", "客厅灯", "客厅", "灯", True),
            DirectoryDevice("offline", "卧室灯", "卧室", "灯", False),
        ]


class Adapter:
    def __init__(self):
        self.requests = []

    async def interpret(self, request):
        self.requests.append(request)
        return None


@pytest.mark.asyncio
async def test_shadow_builds_candidates_from_owner_scoped_directory(caplog) -> None:
    caplog.set_level(logging.INFO)
    adapter = Adapter()
    shadow = ShadowInterpreter(directory=Directory(), adapter=adapter)
    assert (
        await shadow.observe(
            utterance="打开灯", turn_id="turn-1", owner_id="owner-1", companion_id="companion-1"
        )
        is None
    )
    assert [d.device_id for d in adapter.requests[0].devices] == ["lamp-1"]
    assert "home_interpretation_shadow" in caplog.text
    assert "打开灯" not in caplog.text


@pytest.mark.asyncio
async def test_candidate_revision_is_explicit_without_changing_r14_default():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=_reply(revision="7b695ba8")))
    async with httpx.AsyncClient(transport=transport) as client:
        candidate = LayaInterpretationAdapter(client=client, revision="7b695ba8")
        assert (await candidate.interpret(_request())).model_revision == "7b695ba8"
        assert await LayaInterpretationAdapter(client=client).interpret(_request()) is None
