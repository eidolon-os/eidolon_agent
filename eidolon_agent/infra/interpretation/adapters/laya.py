"""Model-first smart-home interpretation over one batched Laya request.

Current-sentence questions retain their frozen input; context questions receive
only the facts they need. No lexical rules decide whether the model may run.
The model selects semantics; deterministic capability/quantity mapping creates
a proposal, and the command boundary validates it before execution.
"""

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

import httpx
from eidolon_sdk.biz.interpretation import (
    ERROR_CONTEXT_TOO_LARGE,
    ERROR_INVALID_PROPOSAL,
    ERROR_INVALID_REQUEST,
    ERROR_TIMEOUT,
    ERROR_UNAVAILABLE,
    Action,
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
    utterance_values,
)

POLICY_VERSION = "laya-smarthome-context-v2"
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
        self._client = create_async_client(
            timeout=5.0, transport=transport, headers=headers, trust_env=False
        )
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
                "intent": {**INTENT_QUESTION},
                "device": {
                    "type": "choice",
                    "instructions": DEVICE_INSTRUCTIONS,
                    "criteria": {
                        **{label: text for label, (text, _c) in options.items()},
                        **DEVICE_EXITS,
                    },
                },
                "action": {**ACTION_QUESTION},
            },
        }
        context_questions = _context_questions(request, options)
        body["questions"].update(context_questions)
        payload = await self.predict(body, timeout_ms=request.timeout_ms)
        result = self._result(request, options, payload)
        if context_questions:
            if (
                not isinstance(payload.get("features"), dict)
                or payload["features"].get("question_state") is not True
            ):
                return result.model_copy(
                    update={
                        "status": "abstained",
                        "proposal": None,
                        "diagnostics": {
                            **result.diagnostics,
                            "reason": "question_state_unsupported",
                        },
                    }
                )
            return _context_result(request, options, payload, result)
        return result

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
                            raise InterpretationError(
                                ERROR_INVALID_PROPOSAL, "laya: response too large"
                            )
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
        for key in ("intent", "device", "action"):
            answer = answers.get(key)
            if not isinstance(answer, dict) or not isinstance(
                answer.get("probabilities", {}), dict
            ):
                raise InterpretationError(ERROR_INVALID_PROPOSAL, f"laya: bad {key} probabilities")
        intent = _choice(answers, "intent", _INTENTS)
        device = _choice(answers, "device", {*options, *DEVICE_EXITS})
        action = _choice(answers, "action", _ACTIONS)
        diagnostics = _diagnostics(payload, answers)
        diagnostics["answers_json"] = json.dumps(
            {
                key: {
                    "choice": answer.get("choice"),
                    "top": sorted(
                        [
                            (label, value)
                            for label, value in (answer.get("probabilities") or {}).items()
                            if type(value) in (int, float)
                            and math.isfinite(value)
                            and 0 <= value <= 1
                        ],
                        key=lambda item: item[1],
                        reverse=True,
                    )[:3],
                }
                for key, answer in answers.items()
                if key in {"intent", "device", "action", "context_device", "pick", "follow"}
                and isinstance(answer, dict)
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
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


PICK_INSTRUCTIONS = "Agent 刚问用户要对哪台设备执行 `context.待执行`。`utterance` 是否明确选了其中一台，且没有改动作？"
FOLLOW_INSTRUCTIONS = (
    "`context.设备` 刚被操作或查询过。`utterance` 是否是对这些设备的一个明确操作？"
)


def _context_questions(request, options):
    context = request.context
    if not context:
        return {}
    state = {"utterance": request.utterance, "context": _model_context(request)}
    questions = {
        "context_device": {
            "type": "choice",
            "instructions": DEVICE_INSTRUCTIONS,
            "criteria": {**{k: v[0] for k, v in options.items()}, **DEVICE_EXITS},
            "state": state,
        }
    }
    previous = context.get("proposal") or {}
    refs = previous.get("targets", [])
    # Keep the frozen c4 continuation question's immediate facts. Older turns
    # remain available to context_device and to the LLM; they are not repeated
    # in every classifier where they contaminate the single-turn task.
    immediate = dict(state["context"])
    immediate.pop("最近对话", None)
    continuation_state = {"utterance": request.utterance, "context": immediate}
    # These are structural capabilities, never a parse of the user's words.
    if (
        context.get("pending")
        and previous.get("target_status") == "ambiguous"
        and previous.get("action")
    ):
        criteria = {label: value[0] for label, value in options.items() if value[1].ref in refs}
        if 2 <= len(criteria) <= 8:
            pick_state = {
                "utterance": request.utterance,
                "context": {k: v for k, v in immediate.items() if k != "设备"},
            }
            questions["pick"] = {
                "type": "choice",
                "instructions": PICK_INSTRUCTIONS,
                "criteria": {
                    **criteria,
                    "取消": "用户明确不要执行这次待确认的操作，也没有提出别的要求",
                    "重新理解": "改了动作或数值、要多台或候选之外的设备、没说清是哪台、提问、闲聊或提出新要求",
                },
                "state": pick_state,
            }
    elif previous.get("target_status") == "resolved" and len(refs) == 1:
        continuation_state["context"].pop("待执行", None)
        questions["follow"] = {
            "type": "choice",
            "instructions": FOLLOW_INSTRUCTIONS,
            "criteria": {
                **ACTION_QUESTION["criteria"],
                "重新理解": "换了或增加设备、只针对其中一部分、撤销刚才的操作、保持不动、提问、闲聊或其他新要求",
            },
            "state": continuation_state,
        }
    return questions


def _prob(answers, key):
    a = answers.get(key, {})
    if not isinstance(a, dict) or not isinstance(a.get("probabilities"), dict):
        return 0.0
    value = a["probabilities"].get(a.get("choice"))
    return (
        float(value)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
        else 0.0
    )


def _context_result(request, options, payload, result):
    """Combine model evidence without a lexical gate or a second model request."""
    answers = payload.get("answers", {})
    diagnostics = dict(result.diagnostics)
    if payload.get("truncated"):
        return result

    def abstain(reason):
        return result.model_copy(
            update={
                "status": "abstained",
                "proposal": None,
                "diagnostics": {**diagnostics, "reason": reason},
            }
        )

    primary = result.proposal
    direct = (
        primary is not None
        and primary.intent != "unrelated"
        and all(_prob(answers, q) >= 0.99 for q in ("intent", "device", "action"))
    )
    # A model disagreement is evidence for escalation, not an instruction to keep old focus.
    pick = answers.get("pick", {}).get("choice")
    if _prob(answers, "pick") >= 0.95 and pick == "取消":
        if direct:
            return abstain("model_disagreement")
        return result.model_copy(
            update={
                "status": "decided",
                "proposal": Proposal(intent="unrelated", target_status="none"),
                "diagnostics": {
                    **diagnostics,
                    "home_decision": "cancelled",
                    "resolution": "pick",
                    "intent_p": _prob(answers, "pick"),
                },
            }
        )
    context = request.context or {}
    previous = context.get("proposal") or {}
    if (
        _prob(answers, "pick") >= 0.95
        and pick in options
        and pick not in {"取消", "重新理解"}
        and options[pick][1].ref in previous.get("targets", [])
    ):
        proposal = Proposal(
            intent="control",
            target_status="resolved",
            targets=(options[pick][1].ref,),
            action=Action.model_validate(previous["action"]),
        )
        if direct and primary != proposal:
            return abstain("model_disagreement")
        return result.model_copy(
            update={
                "status": "decided",
                "proposal": proposal,
                "diagnostics": {
                    **diagnostics,
                    "intent_p": _prob(answers, "pick"),
                    "device_p": _prob(answers, "pick"),
                    "action_p": _prob(answers, "pick"),
                    "resolution": "pick",
                },
            }
        )
    if context.get("pending"):
        # A complete new command may replace pending work only when the
        # continuation classifier independently calls for reinterpretation.
        # Uncertain picks and conflicting selections/cancellations still escalate.
        if direct and pick == "重新理解" and _prob(answers, "pick") >= 0.95:
            return result
        return abstain("pending_not_resolved")
    if direct:
        return result
    follow = answers.get("follow", {}).get("choice")
    device = answers.get("context_device", {}).get("choice")
    if answers.get("device", {}).get("choice") == MULTIPLE:
        return abstain("multiple_devices")
    if (
        answers.get("intent", {}).get("choice") == "控制"
        and _prob(answers, "intent") >= 0.99
        and follow in _ACTIONS
        and _prob(answers, "follow") >= 0.95
        and device in options
        and _prob(answers, "context_device") >= 0.95
        and list(previous.get("targets", [])) == [options[device][1].ref]
    ):
        proposal = _control(request, options, device, _ACTIONS[follow])
        if proposal is not None:
            if (
                primary is not None
                and primary.intent == "control"
                and _prob(answers, "device") >= 0.8
                and primary.targets != proposal.targets
            ):
                return abstain("model_disagreement")
            return result.model_copy(
                update={
                    "status": "decided",
                    "proposal": proposal,
                    "diagnostics": {
                        **diagnostics,
                        "intent_p": _prob(answers, "follow"),
                        "action_p": _prob(answers, "follow"),
                        "device_p": _prob(answers, "context_device"),
                        "resolution": "follow",
                    },
                }
            )
    if primary is not None and primary.intent == "unrelated":
        # An unrelated answer on a fragment can conflict with the context model.
        return abstain("context_not_resolved")
    return result


def _model_context(request: InterpretationRequest) -> dict:
    context = request.context or {}
    names = {c.ref: c.name for c in request.candidates}
    proposal = context.get("proposal") or {}
    refs = proposal.get("targets", context.get("known_targets", []))
    return {
        "最近对话": [
            {"用户": h["utterance"], "助手": h["response"]} for h in context.get("history", [])[-5:]
        ],
        "上一句": context.get("previous_utterance", ""),
        "Agent": context.get("question") or context.get("response", ""),
        "设备": [names[r] for r in refs if r in names],
        "待执行": context.get("pending_action", "")
        if context.get("pending")
        else "无，上一轮已结束",
    }


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
    values = utterance_values(request)
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
        if (
            type(probability) in (int, float)
            and math.isfinite(probability)
            and 0 <= probability <= 1
        ):
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
