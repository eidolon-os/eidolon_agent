"""Constrained smart-home proposal over the shared LLM transport.

The model can only suggest a Proposal. SmartHomeCommand validates candidate
identities and command vocabulary against the current Owner registry before it
asks the Provider to execute anything.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from eidolon_sdk.biz.interpretation import (
    ERROR_INVALID_PROPOSAL,
    ERROR_UNAVAILABLE,
    Action,
    InterpretationError,
    InterpretationRequest,
    Proposal,
)
from eidolon_sdk.biz.smarthome import (
    DEVICE_TYPES,
    SCENE_COMMAND,
    SCENE_KIND,
    SCENE_TRAIT,
    TRAIT_COMMANDS,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eidolon_agent.core.errors import LLMUnavailableError
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.types.llm import LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolSchema
from eidolon_agent.domain.smarthome.context import (
    HomeCancellation,
    HomeClarification,
    HomeUnderstanding,
)
from eidolon_agent.infra.interpretation.adapters.lexicon import (
    SINGULAR_REFERENCE,
    normalize,
    utterance_values,
)
from eidolon_agent.infra.interpretation.adapters.rules import has_explicit_reference
from eidolon_agent.infra.interpretation.adapters.rules import read as read_rules

_log = logging.getLogger(__name__)


class _ProposalArguments(BaseModel):
    """One wire contract for both the advertised tool and response validation."""

    model_config = ConfigDict(extra="forbid")
    proposal: Proposal | None


class _ClarificationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=100)
    targets: tuple[str, ...] = Field(description="已确定或待选择的候选设备 ref；未知时 []，不能只在 question 中写设备名")
    action: Action | None = Field(description="已明确且参数完整的待执行动作；缺少动作或必要参数时 null")


class _CancelArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


_PROPOSAL_TOOL = ToolSchema(
    name="propose_home_action",
    description="返回本轮完整请求的理解：control、query 或 unrelated。取消后提出非家居新请求也在这里返回 unrelated。一个 control 只能表达所有 targets 共享同一个 action；不同动作必须澄清，不能压成一个动作。",
    json_schema=_ProposalArguments.model_json_schema(),
)
_CLARIFICATION_TOOL = ToolSchema(
    name="ask_home_clarification",
    description="执行意图、目标、动作或参数不明确时澄清；只排除部分候选而没有肯定选择也必须澄清，即使只剩一台。多个不同动作请用户拆开。意图未确认或多动作不能表示时 action=null。",
    json_schema=_ClarificationArguments.model_json_schema(),
)
_CANCEL_TOOL = ToolSchema(
    name="cancel_home_command",
    description="仅用于整句话都在取消、拒绝全部候选、禁止操作或保持现状，并且没有任何新请求。若取消后还有新请求（包括非家居请求），必须解释后面的新请求，不能调用此工具。只排除部分候选不等于取消。",
    json_schema=_CancelArguments.model_json_schema(),
)


class LlmHomeFallback:
    def __init__(self, llm: LLMPort) -> None:
        self._llm = llm

    async def propose(self, request: InterpretationRequest, *, context: dict | None = None) -> HomeUnderstanding:
        rooms = {area.area_id: area.name for area in request.areas}
        candidates = [
            {
                "ref": candidate.ref,
                "name": candidate.name,
                "aliases": candidate.aliases,
                "kind": candidate.kind,
                "room": rooms.get(candidate.area_id or ""),
                "commands": {SCENE_TRAIT: {SCENE_COMMAND: {}}} if candidate.kind == SCENE_KIND else {
                    trait: TRAIT_COMMANDS[trait]
                    for trait in DEVICE_TYPES[candidate.kind].traits
                    if TRAIT_COMMANDS[trait]
                } if candidate.kind in DEVICE_TYPES else {},
            }
            for candidate in request.candidates
        ]
        now = datetime.now(UTC)
        messages = [
            ChatMessage(
                id=f"{request.interpretation_id}:system",
                role=MessageRole.SYSTEM,
                content=(
                    "只解释用户这一次智能家居话语。只可选所给 ref 和命令。"
                    "这里只支持即时家居操作；提醒或未来操作不能转成现在执行。"
                    "设备操作必须来自当前用户对助手的实际请求。转述他人要求、引用命令、"
                    "讲述过去的动作、假设或讨论操作方法，本身都不是执行授权。"
                    "只有用户另外明确要求助手现在执行，才能将其中的动作作为控制提案；"
                    "无法判断用户是在转述还是请求执行时，先用 ask_home_clarification 确认意图，"
                    "不要因为句子中出现设备名和动作就执行。这一规则适用于所有设备。"
                    "只调用一个工具返回本次理解结果。控制提案一旦通过设备和参数校验，就会实际执行；不要把它当作无副作用的讨论或草稿。"
                    "能够形成完整提案时用 propose_home_action。"
                    "明确设备时 resolved；多个合理候选时 ambiguous 并列出候选 ref，不猜选一个。"
                    "用户明确要求同时控制多台时，resolved 可包含多个 ref。"
                    "但一个 proposal 只有一个共享 action，所有目标必须使用相同 trait、command 和 slots。"
                    "一次要求多个不同动作时（无论同一台还是不同设备），调用 ask_home_clarification 请用户分开说，"
                    "targets=[]、action=null；不能只执行其中一项，不能把多个动作压成同一个动作。"
                    "用户明确说出不存在的设备时 none；目标或动作缺失、无法确定时，"
                    "用 ask_home_clarification 提问，不调用 propose_home_action。"
                    "追问也必须返回已知 targets 和 action，不得只在问句中列设备名称。"
                    "只差从多个设备中选一个时，保留完整候选和动作；重复模糊请求不丢弃待确认候选。"
                    "仅否定或排除部分候选不构成肯定选择：排除后即使只剩一个，也必须追问确认，不能执行。"
                    "用 ask_home_clarification 保留剩余 targets 与待执行 action，等待用户肯定选择；不得再次询问已排除的候选。"
                    "明确拒绝全部候选且没有新要求时，用 cancel_home_command 结束这次请求。"
                    "澄清工具中，缺少动作或必要参数时 action=null，保留已知 targets。"
                    "control 提案必须有完整 action，即使 target_status=none 也一样；"
                    "目标不存在时用 mention 指明名称，不把其他现有设备当作替代。"
                    "不得把信息不足当成设备不存在。"
                    "明确只是陈述、转述或闲聊，用 unrelated、target_status=none、targets=[]、action=null；"
                    "不要用 proposal=null 表示已判断为非操作请求。"
                    "mention 仅在 target_status=none 时用于不存在的设备名称，其余情况必须省略或为 null。"
                    "origin_area 只帮助解释没有指明房间的请求，不覆盖用户明确说出的房间。"
                    "context 是本次会话近期上下文，可能为空。pending=true 表示仍在补全请求，尚未执行。"
                    "proposal、known_targets、known_action 是已确定的事实；proposal=null 不表示其他事实无效。"
                    "question 是刚问用户的问题，当前 utterance 是对它的回答；用回答补齐缺失项，保留其余已知事实。"
                    "已知目标但缺动作时，当前回答给出动作即可形成完整请求；已知动作但缺目标时同理。"
                    "只有补齐后仍缺信息才继续追问，不重复询问用户已经回答的内容。"
                    "先阅读完整 utterance 再决定工具：只取消且没有新要求时用 cancel_home_command。"
                    "取消后又提出新要求时，旧请求作废，只解释新的要求；"
                    "若新要求不属于当前家居能力（例如讲笑话、聊天），用 propose_home_action 返回 unrelated，不能只答已取消。"
                    "禁止把对一个动作的否定转换成执行相反动作：‘不要关闭’不等于‘打开’，"
                    "‘别调高’不等于‘调低’。只禁止、取消或要求保持现状而没有新的肯定操作时，"
                    "返回 cancel_home_command，不生成任何设备动作。"
                    "一句话中既有被否定的动作又有明确的新指令时，只解释新指令；"
                    "如果无法区分则追问，不能猜测补出动作。"
                    "pending=false 且 proposal 非空时，提案是最近已成功执行或回答的对象。"
                    "pending=false 且 proposal=null 时，previous_utterance 只是上一句对话，"
                    "没有已执行或待执行的动作；可用于理解当前明确请求的指代，但不能把历史内容当作执行授权。"
                    "其中唯一的 targets 设备就是当前焦点；当前话语使用代词或省略目标时沿用它，"
                    "无需再次询问设备。改说另一房间的同类设备时，继承旧目标的设备类别，"
                    "在新房间查找同类设备；多个同类才澄清，不扩展为新房间全部设备。"
                    "焦点有多个目标时，明确复数或全体指代可沿用全体；单数指代必须澄清是哪一台，不能对全体执行。"
                    "当前话语的动作优先，不能照抄历史动作；闲聊或换话题不能触发旧操作。"
                    "无上下文时不能猜代词或省略指向，也不能假定设备所在房间。"
                    "参数 proposal 遵循 intent(control/query/unrelated)、"
                    "target_status(resolved/ambiguous/none)、targets、action(trait,command,slots) "
                    "的结构；slots 是 {name,value} 数组。不要执行命令或编造设备。"
                ),
                created_at=now,
            ),
            ChatMessage(
                id=f"{request.interpretation_id}:user",
                role=MessageRole.USER,
                content=json.dumps(
                    {"utterance": request.utterance, "origin_area": rooms.get(request.origin.area_id or ""),
                     "candidates": candidates, "context": context},
                    ensure_ascii=False,
                ),
                created_at=now,
            ),
        ]
        proposals: list[HomeUnderstanding] = []
        finish = None
        try:
            async for delta in self._llm.stream(
                messages,
                tools=[_PROPOSAL_TOOL, _CLARIFICATION_TOOL, _CANCEL_TOOL],
                temperature=0,
                max_tokens=400,
                request_id=request.interpretation_id,
            ):
                call = delta.tool_call
                if delta.finish is not None:
                    finish = delta.finish
                if call is not None:
                    if call.name == _PROPOSAL_TOOL.name:
                        proposals.append(_ProposalArguments.model_validate(call.arguments).proposal)
                    elif call.name == _CLARIFICATION_TOOL.name:
                        clarification = _ClarificationArguments.model_validate(call.arguments)
                        proposals.append(HomeClarification(clarification.question, clarification.targets, clarification.action))
                    elif call.name == _CANCEL_TOOL.name:
                        _CancelArguments.model_validate(call.arguments)
                        proposals.append(HomeCancellation())
                    else:
                        raise InterpretationError(ERROR_INVALID_PROPOSAL, "unexpected_tool")
        except LLMUnavailableError as exc:
            raise InterpretationError(ERROR_UNAVAILABLE, "home LLM unavailable") from exc
        except ValidationError as exc:
            # Do not log raw arguments/user text. Preserve the field and error
            # category so malformed output is distinguishable from abstention.
            errors = [{"loc": list(e["loc"]), "type": e["type"], "message": e["msg"]} for e in exc.errors()]
            _log.warning("home fallback turn=%s rejected=%s", request.interpretation_id, errors)
            raise InterpretationError(ERROR_INVALID_PROPOSAL, "invalid_tool_arguments") from exc
        if finish not in (LLMFinishReason.STOP, LLMFinishReason.TOOL_CALLS):
            raise InterpretationError(ERROR_INVALID_PROPOSAL, f"incomplete_stream:{finish}")
        if len(proposals) > 1:
            raise InterpretationError(ERROR_INVALID_PROPOSAL, "multiple_proposals")
        if not proposals:
            _log.info("home fallback turn=%s outcome=abstained reason=no_tool_call", request.interpretation_id)
            return None
        result = proposals[0]
        previous = (context or {}).get("proposal") or {}
        focus = tuple(previous.get("targets") or (context or {}).get("known_targets") or ())
        refers_to_one = SINGULAR_REFERENCE.search(normalize(request.utterance))
        if ((isinstance(result, Proposal) and result.intent != "unrelated")
                or isinstance(result, HomeClarification)) and refers_to_one:
            if not focus and not has_explicit_reference(request, include_areas=False):
                # A room plus a demonstrative is not a device identity, including
                # after an unavailable turn, expiry or cancellation cleared focus.
                _log.info("home fallback turn=%s outcome=clarification reason=missing_focus", request.interpretation_id)
                return HomeClarification("你指的是哪台设备？请说出设备名称")
            if (len(focus) > 1 and not (context or {}).get("pending")
                    and not has_explicit_reference(request)):
                # No affirmative selection exists. Do not let the LLM choose a
                # member or expand a singular reference into the whole focus.
                _log.info("home fallback turn=%s outcome=clarification reason=singular_focus", request.interpretation_id)
                return HomeClarification("你指的是哪一台设备？", focus, result.action)
        if isinstance(result, HomeCancellation):
            _log.info("home fallback turn=%s outcome=cancelled", request.interpretation_id)
            return result
        if isinstance(result, HomeClarification):
            _log.info("home fallback turn=%s outcome=clarification", request.interpretation_id)
            return result
        proposal = result
        if proposal is None:
            _log.info("home fallback turn=%s outcome=abstained reason=null_proposal", request.interpretation_id)
            return None
        if proposal.intent == "control":
            if read_rules(request) == "multiple_commands":
                _log.info("home fallback turn=%s outcome=clarification reason=multiple_commands", request.interpretation_id)
                return HomeClarification("一次请说一个操作，要先执行哪一个？")
            if _absolute_value_disagrees(request, proposal):
                _log.info("home fallback turn=%s outcome=clarification reason=value_disagreement", request.interpretation_id)
                return HomeClarification("请确认要设为多少？", proposal.targets)
        _log.info(
            "home fallback turn=%s outcome=proposed intent=%s target_status=%s targets=%s action=%s",
            request.interpretation_id, proposal.intent, proposal.target_status, proposal.targets,
            (proposal.action.trait, proposal.action.command) if proposal.action else None,
        )
        return proposal


def _absolute_value_disagrees(request: InterpretationRequest, proposal: Proposal) -> bool:
    """An absolute setting must not silently change a single spoken amount.

    Like Laya, the LLM cannot clamp a spoken amount to make it executable.
    Disagreement asks for clarification, never rewrites the command: the amount
    might belong to a relative expression. Complex quantities remain undecided
    by this check; this is not a general-purpose semantic parser.
    """
    action = proposal.action
    if action is None or len(action.slots) != 1:
        return False
    slot = action.slots[0]
    units = {
        ("set", "value"): (None, "percent"),
        ("set_target", "celsius"): (None, "celsius"),
    }.get((action.command, slot.name))
    if units is None:
        return False
    values = utterance_values(request)
    quantity = values.quantity
    if values.problem is not None or quantity is None or quantity.unit not in units:
        return False
    return slot.value != quantity.value
