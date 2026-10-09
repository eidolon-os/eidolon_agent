"""Explicit head actions use normal tool dispatch, not expression intentions."""

import hashlib

from eidolon_sdk.biz.presentation.motion import (
    STACKCHAN_HEAD_TOOL,
    MotionRequest,
    StackChanHeadAction,
)
from pydantic import ValidationError

from eidolon_agent.core.types.tool import Permission, ToolResult, ToolSchema


class StackChanHeadTool:
    schema = ToolSchema(
        name=STACKCHAN_HEAD_TOOL,
        description=(
            "Execute an explicitly requested head action on the current StackChan. "
            "点头=nod; 摇头=shake (not decline); 左看/右看=look_left/look_right; "
            "抬头/低头=look_up/look_down; 回正=home; 停止=stop. "
            "Left/right refer to the robot, clarify ambiguous directions. "
            "Only repeat nod/shake 1-3 times. For a short combination call sequentially, "
            "stopping on failure. Never execute negated, quoted or hypothetical actions. "
            "There are no supported leg, base-lift or light actions. "
            "Wait for this result before claiming execution; software_sequence is NOT "
            "measured physical arrival. Do not add a second head gesture as confirmation."
        ),
        json_schema=StackChanHeadAction.model_json_schema(),
        permissions=frozenset({Permission.SYSTEM}),
        side_effect=True,
        timeout_s=12,
    )

    def __init__(self, executor):
        self._executor = executor

    async def invoke(self, call, *, ctx):
        try:
            action = StackChanHeadAction.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(call.id, self.schema.name, False, error_code="INVALID_ARGUMENT")
        digest = hashlib.sha256(f"{ctx.session_id}:{ctx.turn_id}:{call.id}".encode()).hexdigest()[
            :32
        ]
        request = MotionRequest(turn_id=ctx.turn_id, command_id=f"motion:{digest}", action=action)
        receipt = await self._executor.execute(request)
        completed = receipt.status == "completed"
        return ToolResult(
            call.id,
            self.schema.name,
            completed,
            content=receipt.model_dump(mode="json"),
            error_code=None if completed else (receipt.reason or receipt.status),
            metadata={
                "external_action": True,
                "explicit_motion": True,
                "outcome_state": "completed" if completed else receipt.status,
                "completion_basis": receipt.completion_basis,
            },
        )
