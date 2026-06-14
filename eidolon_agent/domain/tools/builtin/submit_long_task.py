"""``submit_long_task`` — enqueue async work for the local mementos worker."""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from eidolon_agent.core.ports.long_tasks import LongTaskQueueFullError, LongTaskSubmitter
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.long_task import (
    LongTaskRecord,
    LongTaskStatus,
    session_key_for,
    task_key_for,
)
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema


class SubmitLongTaskTool:
    schema = ToolSchema(
        name="submit_long_task",
        description=(
            "Submit an asynchronous long-running task to the local background queue. "
            "A worker will call the mementos coworker and update progress/results later. "
            "Use this only when the user asks for multi-step work, external follow-up, "
            "research/booking/automation, or any task whose final result should arrive "
            "later. Do not use it for ordinary conversation, quick questions, or anything "
            "you can answer immediately. After calling it, do not invent the final result; "
            "tell the user the task has started and wait for progress/result events."
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
                    "description": (
                        "Urgency label: low, normal, high, or urgent. "
                        "Defaults to normal."
                    ),
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
        timeout_s=1.0,
    )

    def __init__(self, long_task_submitter: LongTaskSubmitter | None = None) -> None:
        self._submitter = long_task_submitter

    async def invoke(self, call: ToolCall, *, ctx: ToolInvocationContext) -> ToolResult:
        if self._submitter is None:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="long_task_submitter_unavailable",
                error_message="Long task submitter not wired",
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
        progress_subject = f"long_task.progress.{task_id}"
        now = _localized_now(ctx.caller.locale)
        task_date = now.date().isoformat()
        session_key = session_key_for(ctx.caller.user_id, now.date())
        task_key = task_key_for(session_key, task_id)
        payload = {
            "task_id": task_id,
            "tenant_id": ctx.caller.tenant_id,
            "user_id": ctx.caller.user_id,
            "agent_instance_id": ctx.caller.agent_instance_id,
            "conversation_id": ctx.conversation_id,
            "session_id": ctx.session_id,
            "turn_id": ctx.turn_id,
            "trace_id": ctx.caller.trace_id,
            "tool_call_id": call.id,
            "session_key": session_key,
            "task_key": task_key,
            "task_date": task_date,
            "mementos_session_id": session_key,
            "natural_language": task,
            "source_user_text": ctx.user_text or "",
            "task_type": call.arguments.get("task_type") or "other",
            "urgency": call.arguments.get("urgency") or "normal",
            "expected_output": call.arguments.get("expected_output") or "",
            "context_summary": call.arguments.get("context_summary") or "",
            "progress_subject": progress_subject,
        }
        record = LongTaskRecord(
            id=task_id,
            provider="mementos",
            status=LongTaskStatus.ACCEPTED,
            tenant_id=ctx.caller.tenant_id,
            user_id=ctx.caller.user_id,
            agent_instance_id=ctx.caller.agent_instance_id,
            conversation_id=ctx.conversation_id,
            turn_id=ctx.turn_id,
            session_id=ctx.session_id,
            trace_id=ctx.caller.trace_id,
            tool_call_id=call.id,
            session_key=session_key,
            task_date=task_date,
            task_key=task_key,
            task=task,
            user_text=ctx.user_text or "",
            task_type=payload["task_type"],
            urgency=payload["urgency"],
            expected_output=payload["expected_output"],
            context_summary=payload["context_summary"],
            request_payload=payload,
            callback_subject=progress_subject,
        )
        try:
            await self._submitter.submit(record)
        except LongTaskQueueFullError as exc:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="long_task_queue_full",
                error_message=str(exc),
            )
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="long_task_submit_failed",
                error_message=str(exc),
            )
        return ToolResult(
            call_id=call.id,
            name=self.schema.name,
            ok=True,
            content={
                "accepted": True,
                "task_id": task_id,
                "progress_subject": progress_subject,
                "session_key": session_key,
                "task_key": task_key,
                "task_date": task_date,
            },
        )


def _localized_now(locale: str) -> datetime:
    timezone_name = {
        "zh-CN": "Asia/Shanghai",
        "zh-HK": "Asia/Hong_Kong",
        "zh-TW": "Asia/Taipei",
        "ja-JP": "Asia/Tokyo",
        "en-US": "America/Los_Angeles",
    }.get(locale, "UTC")
    return datetime.now(ZoneInfo(timezone_name))
