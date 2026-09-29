"""Shadow adapter for the laya model service (``eidolon_models/laya``).

laya is a choice encoder: it picks among options and never extracts numbers.
This adapter asks the smart-home scenario's three questions word for word
(``train/scenarios/smart-home/scenario.yaml``: intent / device / action, the
device options being ``{name: "room·type"}`` plus two fixed exits) over
``POST /v1/systemone``, maps the answers onto a Proposal, and takes numbers and
modes from the shared lexicon, as the rules adapter does.

Known gap: laya was trained on product types (吸顶灯, 空气净化器) while a
Candidate only carries the SDK device type, so options read ``客厅·灯`` or
``客厅·风扇/净化/加湿``; the device name still carries most of the signal.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from eidolon_sdk.biz.interpretation import (
    ERROR_CONTEXT_TOO_LARGE,
    ERROR_INVALID_PROPOSAL,
    ERROR_INVALID_REQUEST,
    ERROR_TIMEOUT,
    ERROR_UNAVAILABLE,
    Candidate,
    InterpretationError,
    InterpretationRequest,
    InterpretationResult,
    Proposal,
)
from eidolon_sdk.biz.smarthome import DEVICE_TYPES, SCENE_KIND
from eidolon_sdk.core.http import create_async_client

from eidolon_agent.infra.interpretation.adapters.lexicon import (
    Verb,
    command_for,
    generic_command,
    normalize,
    read_values,
    take,
)

POLICY_VERSION = "laya-smarthome-v1"
MODEL_VERSION = "laya"

MULTIPLE = "多个设备或整屋"
NO_DEVICE = "没有对应的设备"
DEVICE_EXITS = {
    MULTIPLE: "同时涉及多台设备，或整屋的场景模式",
    NO_DEVICE: "家里没有能满足这个要求的设备",
}
INTENT_QUESTION = {
    "type": "choice",
    "instructions": "`utterance` 是在让智能家居做什么？",
    "criteria": {
        "控制": "要求改变家里设备的状态：打开、关闭、调节、设定、启动、暂停、上锁",
        "查询": "询问家里设备的状态或读数，不改变任何设备",
        "无关": "与控制或查询家里的设备无关：闲聊、常识、陈述、购物",
    },
}
DEVICE_INSTRUCTIONS = "`utterance` 说的是家里的哪台设备？"
ACTION_QUESTION = {
    "type": "choice",
    "instructions": "`utterance` 要对设备做什么操作？",
    "criteria": {
        "打开或启动": "开启、启动、开始运行",
        "关闭或停止": "关掉、停止、断电、收起",
        "调高或增大": "调亮、升温、调大音量或档位",
        "调低或减小": "调暗、降温、调小、降下",
        "设为指定的数值或模式": "设到具体的温度、档位、百分比或模式",
        "暂停": "暂时停下，之后再继续",
        "上锁": "锁门、上锁",
    },
}
_INTENTS = {"控制": "control", "查询": "query", "无关": "unrelated"}
_ACTIONS = {
    "打开或启动": Verb("on"),
    "关闭或停止": Verb("off"),
    "调高或增大": Verb("up"),
    "调低或减小": Verb("down"),
    "设为指定的数值或模式": Verb("set"),
    "暂停": Verb("pause"),
    "上锁": Verb("lock"),
}


class LayaInterpreter:
    """Implements the InteractionInterpretation port over laya's HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        policy_version: str = POLICY_VERSION,
        model_version: str = MODEL_VERSION,
    ) -> None:
        self._url = base_url.rstrip("/") + "/v1/systemone"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # A host-local model service: never through the operator's HTTP proxy.
        self._client = create_async_client(timeout=5.0, transport=transport, headers=headers, trust_env=False)
        self._policy_version = policy_version
        self._model_version = model_version

    async def aclose(self) -> None:
        await self._client.aclose()

    async def interpret(self, request: InterpretationRequest) -> InterpretationResult:
        if request.domain != "smarthome":
            raise InterpretationError(ERROR_INVALID_REQUEST, f"domain {request.domain!r}")
        options = device_options(request)
        body = {
            "state": {"utterance": request.utterance},
            "questions": {
                "intent": INTENT_QUESTION,
                "device": {
                    "type": "choice",
                    "instructions": DEVICE_INSTRUCTIONS,
                    "criteria": {
                        **{label: text for label, (text, _c) in options.items()},
                        **DEVICE_EXITS,
                    },
                },
                "action": ACTION_QUESTION,
            },
        }
        payload = await self.predict(body, timeout_ms=request.timeout_ms)
        return self._result(request, options, payload)

    async def predict(self, body: dict, *, timeout_ms: int) -> dict:
        """One transport for single-sentence and bounded continuation questions."""
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                async with self._client.stream(
                    "POST", self._url, json=body, timeout=timeout_ms / 1000
                ) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 256 * 1024:
                            raise InterpretationError(ERROR_INVALID_PROPOSAL, "laya: response too large")
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise InterpretationError(ERROR_TIMEOUT, f"laya: {exc!r}") from exc
        except httpx.TransportError as exc:
            raise InterpretationError(ERROR_UNAVAILABLE, f"laya: {exc!r}") from exc
        if response.status_code != 200:
            raise _status_error(httpx.Response(response.status_code, content=bytes(content)))
        try:
            payload = json.loads(content)
        except ValueError as exc:
            raise InterpretationError(ERROR_INVALID_PROPOSAL, "laya: reply is not JSON") from exc
        if not isinstance(payload, dict):
            raise InterpretationError(ERROR_INVALID_PROPOSAL, "laya: reply is not an object")
        return payload

    def _result(
        self,
        request: InterpretationRequest,
        options: dict[str, tuple[str, Candidate]],
        payload: Any,
    ) -> InterpretationResult:
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            raise InterpretationError(ERROR_INVALID_PROPOSAL, "laya: no answers")
        intent = _choice(answers, "intent", _INTENTS)
        device = _choice(answers, "device", {*options, *DEVICE_EXITS})
        action = _choice(answers, "action", _ACTIONS)
        diagnostics = _diagnostics(payload, answers)
        model = payload.get("model")
        model_version = model if isinstance(model, str) and model.strip() else self._model_version
        revision = payload.get("revision")
        if isinstance(revision, str) and revision.strip():
            model_version = f"{model_version}@{revision.strip()}"

        proposal: Proposal | None
        if payload.get("truncated"):
            proposal = None
            diagnostics["reason"] = "truncated_input"
        elif _INTENTS[intent] == "unrelated":
            proposal = Proposal(intent="unrelated", target_status="none")
        elif device == MULTIPLE:
            proposal = None  # several devices or a scene: laya cannot name which
            diagnostics["reason"] = "multiple_devices"
        elif _INTENTS[intent] == "query":
            refs = () if device == NO_DEVICE else (options[device][1].ref,)
            proposal = Proposal(
                intent="query", target_status="resolved" if refs else "none", targets=refs
            )
        else:
            proposal = _control(request, options, device, _ACTIONS[action])
            if proposal is None:
                diagnostics["reason"] = "unsupported_command"

        return InterpretationResult(
            interpretation_id=request.interpretation_id,
            status="abstained" if proposal is None else "decided",
            proposal=proposal,
            policy_version=self._policy_version,
            model_version=model_version[:128],
            diagnostics=diagnostics,
        )


def device_options(request: InterpretationRequest) -> dict[str, tuple[str, Candidate]]:
    """``{option label: ("room·type", candidate)}``; scenes are laya's 整屋 exit."""
    rooms = {area.area_id: area.name for area in request.areas}
    options: dict[str, tuple[str, Candidate]] = {}
    for candidate in request.candidates:
        if candidate.kind == SCENE_KIND:
            continue
        spec = DEVICE_TYPES.get(candidate.kind)
        kind = spec.label if spec is not None else candidate.kind
        room = rooms.get(candidate.area_id or "")
        text = f"{room}·{kind}" if room else kind
        label = candidate.name
        if label in options or label in DEVICE_EXITS:
            label = f"{candidate.name}（{room or candidate.ref}）"
        if label in options:
            label = f"{candidate.name}（{candidate.ref}）"
        options[label] = (text, candidate)
    return options


def _control(
    request: InterpretationRequest,
    options: dict[str, tuple[str, Candidate]],
    device: str,
    verb: Verb,
) -> Proposal | None:
    # Numbers come from the lexicon with device names blanked, so 3号灯 is no value.
    names = {normalize(n): None for c in request.candidates for n in (c.name, *c.aliases)}
    text, _names = take(normalize(request.utterance), names)
    _text, values = read_values(text)
    if device == NO_DEVICE:
        action = generic_command(verb, values)
        if action is None:
            return None
        return Proposal(intent="control", target_status="none", action=action)
    candidate = options[device][1]
    action = command_for(candidate.kind, verb, values)
    if action is None:
        return None
    return Proposal(
        intent="control", target_status="resolved", targets=(candidate.ref,), action=action
    )


def _choice(answers: dict, question: str, allowed) -> str:
    answer = answers.get(question)
    choice = answer.get("choice") if isinstance(answer, dict) else None
    if not isinstance(choice, str) or choice not in allowed:
        raise InterpretationError(ERROR_INVALID_PROPOSAL, f"laya: bad {question} answer")
    return choice


def _diagnostics(payload: dict, answers: dict) -> dict[str, bool | int | float | str]:
    out: dict[str, bool | int | float | str] = {}
    for question in ("intent", "device", "action"):
        answer = answers[question]
        probability = (answer.get("probabilities") or {}).get(answer["choice"])
        if isinstance(probability, int | float) and not isinstance(probability, bool):
            out[f"{question}_p"] = float(probability)
    backend = payload.get("backend")
    if isinstance(backend, str):
        out["backend"] = backend[:64]
    timing = payload.get("timing_ms")
    if isinstance(timing, dict) and isinstance(timing.get("total"), int | float):
        out["server_ms"] = float(timing["total"])
    if payload.get("truncated"):
        out["truncated"] = True
    return out


def _status_error(response: httpx.Response) -> InterpretationError:
    try:
        code = str((response.json().get("error") or {}).get("code") or "")
    except (ValueError, AttributeError):
        code = ""
    detail = f"laya HTTP {response.status_code} {code}".strip()
    if response.status_code == 400:
        return InterpretationError(ERROR_INVALID_REQUEST, detail)
    if response.status_code == 413:
        return InterpretationError(ERROR_CONTEXT_TOO_LARGE, detail)
    return InterpretationError(ERROR_UNAVAILABLE, detail)
