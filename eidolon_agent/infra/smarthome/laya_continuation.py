"""Bounded c4 continuation adapter; borrows the existing Laya transport.

Contract: Models 5d3cc2b, continuation.json for 7b695ba8;
LABELING blob 70f78f872278880c38ac421c8fac2872184c08a9, sections 1–2.
No runtime import of training code and no model-owned conversation state.
"""

from __future__ import annotations

import logging
import math

from eidolon_sdk.biz.interpretation import InterpretationRequest, Proposal

from eidolon_agent.domain.smarthome.context import HomeCancellation, HomeUnderstanding
from eidolon_agent.infra.interpretation.adapters.laya import (
    _ACTIONS,
    ACTION_QUESTION,
    LayaInterpreter,
    _control,
    device_options,
)

_log = logging.getLogger(__name__)
MODEL_REVISION = "7b695ba8"
PICK = "Agent 刚问用户要对哪台设备执行 `context.待执行`。`utterance` 是否明确选了其中一台，且没有改动作？"
FOLLOW = "`context.设备` 刚被操作或查询过。`utterance` 是否是对这些设备的一个明确操作？"
PICK_EXITS = {
    "取消": "用户明确不要执行这次待确认的操作，也没有提出别的要求",
    "重新理解": "改了动作或数值、要多台或候选之外的设备、没说清是哪台、提问、闲聊或提出新要求",
}
FOLLOW_EXIT = "换了或增加设备、只针对其中一部分、撤销刚才的操作、保持不动、提问、闲聊或其他新要求"
# This is the calibrated question vocabulary, not a second device capability registry.
# Actual commands are constructed by the existing lexicon and revalidated by Hub.
FOLLOW_KINDS = {
    "打开或启动": {
        "light",
        "switch",
        "climate",
        "water_heater",
        "cover",
        "fan",
        "media",
        "appliance",
    },
    "关闭或停止": {
        "light",
        "switch",
        "climate",
        "water_heater",
        "cover",
        "fan",
        "media",
        "appliance",
    },
    "调高或增大": {"light", "climate", "water_heater", "media"},
    "调低或减小": {"light", "climate", "water_heater", "media"},
    "设为指定的数值或模式": {"light", "climate", "water_heater", "cover", "fan", "media"},
    "暂停": {"appliance"},
}


class LayaHomeContinuation:
    def __init__(self, laya: LayaInterpreter, *, revision: str) -> None:
        if revision != MODEL_REVISION:
            raise ValueError("unsupported Laya continuation contract revision")
        self._laya, self._revision = laya, revision

    async def propose(
        self, request: InterpretationRequest, *, context: dict | None = None
    ) -> HomeUnderstanding:
        if not context or request.domain != "smarthome":
            return None
        try:
            previous = Proposal.model_validate(context.get("proposal"))
        except ValueError:
            return None
        by_ref = {c.ref: c for c in request.candidates}
        if not previous.targets or any(ref not in by_ref for ref in previous.targets):
            return None
        candidates = tuple(by_ref[ref] for ref in previous.targets)
        if any(c.kind in {"lock", "sensor", "scene"} for c in candidates):
            return None
        bounded = request.model_copy(update={"candidates": candidates})
        options = device_options(bounded)
        if len(options) != len(candidates):
            return None
        state = {
            "utterance": request.utterance,
            "context": {"上一句": context["previous_utterance"]},
        }
        pending = context.get("pending") is True
        if pending:
            if (
                previous.intent != "control"
                or previous.target_status != "ambiguous"
                or not 2 <= len(candidates) <= 8
                or not context.get("pending_action")
            ):
                return None
            # Order words only have meaning against precisely the question shown.
            # Ambiguous duplicate names and truncated questions go to the LLM.
            if context.get("question") != "、".join(options) + "，要哪一个？":
                return None
            if any(label in PICK_EXITS for label in options):
                return None
            key, instructions = "pick", PICK
            criteria = {**{label: text for label, (text, _) in options.items()}, **PICK_EXITS}
            state["context"].update(
                {"Agent": context["question"], "待执行": context["pending_action"]}
            )
        else:
            if (
                previous.target_status != "resolved"
                or not 1 <= len(candidates) <= 3
                or not context.get("response")
            ):
                return None
            key, instructions = "follow", FOLLOW
            criteria = {
                name: ACTION_QUESTION["criteria"][name]
                for name, kinds in FOLLOW_KINDS.items()
                if all(c.kind in kinds for c in candidates)
            }
            if not criteria:
                return None
            criteria["重新理解"] = FOLLOW_EXIT
            state["context"].update(
                {"Agent": context["response"], "设备": [c.name for c in candidates]}
            )
        payload = await self._laya.predict(
            {
                "state": state,
                "questions": {
                    key: {"type": "choice", "instructions": instructions, "criteria": criteria}
                },
            },
            timeout_ms=request.timeout_ms,
        )
        if payload.get("revision") != self._revision or payload.get("truncated"):
            return None
        answers = payload.get("answers")
        answer = answers.get(key) if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("type") != "choice":
            return None
        choice = answer.get("choice")
        probabilities = answer.get("probabilities")
        if (
            not isinstance(choice, str)
            or choice not in criteria
            or not isinstance(probabilities, dict)
        ):
            return None
        probability = probabilities.get(choice)
        if (
            not isinstance(probability, int | float)
            or isinstance(probability, bool)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            return None
        _log.info(
            "home continuation turn=%s question=%s model=%s probability=%.4f",
            request.interpretation_id,
            key,
            self._revision,
            probability,
        )
        if pending and choice == "取消" and probability >= 0.5:
            return HomeCancellation()
        if choice in {"取消", "重新理解"} or probability < 0.95:
            return None
        if pending:
            return previous.model_copy(
                update={"target_status": "resolved", "targets": (options[choice][1].ref,)}
            )
        proposals = [_control(bounded, options, label, _ACTIONS[choice]) for label in options]
        if any(p is None for p in proposals) or any(
            p.action != proposals[0].action for p in proposals
        ):
            return None
        # One Proposal carries one shared action. Heterogeneous actions require LLM planning.
        return Proposal(
            intent="control",
            target_status="resolved",
            targets=previous.targets,
            action=proposals[0].action,
        )
