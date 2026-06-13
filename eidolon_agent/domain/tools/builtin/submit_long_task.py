"""``submit_long_task`` — hand off async work to the workstation/mementos agent."""

from __future__ import annotations

import uuid

from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema
from eidolon_agent.core.types.topics import Topics


class SubmitLongTaskTool:
    schema = ToolSchema(
        name="submit_long_task",
        description=(
            "Submit an asynchronous long-running task to the external mementos/workstation "
            "agent. Use this only when the user asks for multi-step work, external "
            "follow-up, research/booking/automation, or any task whose final result should "
            "arrive later. Do not use it for ordinary conversation, quick questions, or "
            "anything you can answer immediately. After calling it, do not invent the final "
            "result; tell the user the task has started and wait for progress/result events."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The concrete user request to perform asynchronously.",
                },
                "task_type": {
                    "type": "string",
                    "description": (
                        "Short category such as research, booking, scheduling, document_work, "
                        "automation, or other."
                    ),
                },
                "urgency": {
                    "type": "string",
                    "description": "Urgency label: low, normal, high, or urgent. Defaults to normal.",
                },
                "expected_output": {
                    "type": "string",
                    "description": "What the user expects back when the task completes.",
                },
                "context_summary": {
                    "type": "string",
                    "description": (
                        "Brief relevant context from this conversation needed by the worker. "
                        "Do not include unrelated private details."
                    ),
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
        permissions=frozenset({Permission.SYSTEM}),
        side_effect=True,
        timeout_s=0.5,
    )

    def __init__(self, event_bus=None) -> None:
        self._bus = event_bus

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._bus is None:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="event_bus_unavailable",
                error_message="EventBus not wired",
            )

        task = str(call.arguments.get("task") or "").strip()
        if not task:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="invalid_long_task",
                error_message="task is required",
            )

        task_id = uuid.uuid4().hex
        progress_subject = Topics.workstation_progress(task_id)
        payload = {
            "task_id": task_id,
            "tenant_id": ctx.caller.tenant_id,
            "user_id": ctx.caller.user_id,
            "conversation_id": ctx.conversation_id,
            "session_id": ctx.session_id,
            "turn_id": ctx.turn_id,
            "trace_id": ctx.caller.trace_id,
            "natural_language": task,
            "source_user_text": ctx.user_text or "",
            "task_type": call.arguments.get("task_type") or "other",
            "urgency": call.arguments.get("urgency") or "normal",
            "expected_output": call.arguments.get("expected_output") or "",
            "context_summary": call.arguments.get("context_summary") or "",
            "progress_subject": progress_subject,
        }
        await self._bus.publish(
            Event(
                subject=Topics.workstation_submit(),
                payload=payload,
                trace_id=ctx.caller.trace_id,
                source="tool.submit_long_task",
                metadata={"msg_id": task_id},
            ),
            persistent=True,
        )
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={
                "accepted": True,
                "task_id": task_id,
                "progress_subject": progress_subject,
            },
        )
