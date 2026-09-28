"""Constrained smart-home proposal over the shared LLM transport.

The model can only suggest a Proposal. SmartHomeCommand validates candidate
identities and command vocabulary against the current Owner registry before it
asks the Provider to execute anything.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from eidolon_sdk.biz.interpretation import (
    ERROR_UNAVAILABLE,
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

from eidolon_agent.core.errors import LLMUnavailableError
from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.tool import ToolSchema

_PROPOSAL_TOOL = ToolSchema(
    name="propose_home_action",
    description="提出一次家居意图、目标和动作；此工具不会执行设备命令。",
    json_schema={
        "type": "object",
        "properties": {"proposal": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": ["control", "query", "unrelated"]},
                "target_status": {"type": "string", "enum": ["resolved", "ambiguous", "none"]},
                "targets": {"type": "array", "items": {"type": "string"}},
                "action": {"type": "object", "properties": {
                    "trait": {"type": "string"}, "command": {"type": "string"},
                    "slots": {"type": "array", "items": {"type": "object", "properties": {
                        "name": {"type": "string"}, "value": {"type": ["string", "number", "boolean"]},
                    }, "required": ["name", "value"]}},
                }, "required": ["trait", "command"]},
                "mention": {"type": "string"},
            },
            "required": ["intent", "target_status", "targets"],
        }},
        "required": ["proposal"],
        "additionalProperties": False,
    },
)


class LlmHomeFallback:
    def __init__(self, llm: LLMPort) -> None:
        self._llm = llm

    async def propose(self, request: InterpretationRequest) -> Proposal | None:
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
                    "无法确定设备或动作时不要调用工具。需要执行时调用一次 propose_home_action，"
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
                     "candidates": candidates},
                    ensure_ascii=False,
                ),
                created_at=now,
            ),
        ]
        proposals: list[Proposal] = []
        try:
            async for delta in self._llm.stream(
                messages,
                tools=[_PROPOSAL_TOOL],
                temperature=0,
                max_tokens=400,
                request_id=request.interpretation_id,
            ):
                call = delta.tool_call
                if call is not None:
                    if call.name != _PROPOSAL_TOOL.name:
                        return None
                    proposals.append(Proposal.model_validate(call.arguments.get("proposal")))
        except LLMUnavailableError as exc:
            raise InterpretationError(ERROR_UNAVAILABLE, "home LLM unavailable") from exc
        except ValueError:
            return None
        return proposals[0] if len(proposals) == 1 else None
