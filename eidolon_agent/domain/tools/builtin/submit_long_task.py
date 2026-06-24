"""Delegate non-realtime work to the local coworker queue."""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from eidolon_sdk.long_tasks import progress_subject_for

from eidolon_agent.core.ports.long_tasks import LongTaskQueueFullError, LongTaskSubmitter
from eidolon_agent.core.ports.tool import ToolInvocationContext
from eidolon_agent.core.types.long_task import (
    LongTaskRecord,
    LongTaskStatus,
    session_key_for,
    task_key_for,
)
from eidolon_agent.core.types.tool import Permission, ToolCall, ToolResult, ToolSchema

DELEGATE_TO_COWORKER_TOOL = "delegate_to_coworker"


class SubmitLongTaskTool:
    def __init__(
        self,
        long_task_submitter: LongTaskSubmitter | None = None,
    ) -> None:
        self.schema = _delegate_schema()
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

        args = _normalize_arguments(call.arguments)
        instruction = args["instruction"]
        if not instruction:
            return ToolResult(
                call_id=call.id,
                name=self.schema.name,
                ok=False,
                error_code="invalid_long_task",
                error_message="instruction is required",
            )

        task_id = uuid.uuid4().hex
        progress_subject = progress_subject_for(task_id)
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
            "title": args["title"],
            "instruction": instruction,
            "natural_language": instruction,
            "source_user_text": ctx.user_text or "",
            "task_type": args["task_type"],
            "urgency": args["urgency"],
            "expected_result": args["expected_result"],
            "expected_output": args["expected_result"],
            "context": args["context"],
            "context_summary": args["context"],
            "progress_subject": progress_subject,
        }
        record = LongTaskRecord(
            id=task_id,
            provider="mementos",
            status=LongTaskStatus.ACCEPTED,
            tenant_id=ctx.caller.tenant_id,
            user_id=ctx.caller.user_id,
            agent_instance_id=ctx.caller.agent_instance_id,
            device_id=ctx.caller.identity.device_id,
            conversation_id=ctx.conversation_id,
            turn_id=ctx.turn_id,
            session_id=ctx.session_id,
            trace_id=ctx.caller.trace_id,
            tool_call_id=call.id,
            session_key=session_key,
            task_date=task_date,
            task_key=task_key,
            task=instruction,
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


def _delegate_schema() -> ToolSchema:
    return ToolSchema(
        name=DELEGATE_TO_COWORKER_TOOL,
        description=(
            "Delegate work to a background coworker when the realtime agent should not "
            "complete it inside the current response. Use this for complex, multi-step, "
            "slow, external, or follow-up work such as research, booking, scheduling, "
            "document preparation, automation, or anything whose final result should "
            "arrive later. Do not use it for ordinary conversation, quick answers, or "
            "tasks the realtime agent can finish now. After calling it, only tell the "
            "user the coworker has started; do not invent the final result."
        ),
        json_schema={
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": (
                        "The complete instruction for the background coworker. Include "
                        "the concrete objective and any constraints needed to do the work."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "A short user-facing title for tracking this delegated task, "
                        "for example '整理项目资料' or '查询上海航班'."
                    ),
                },
                "task_type": {
                    "type": "string",
                    "description": (
                        "Short category: research, booking, scheduling, document_work, "
                        "automation, or other."
                    ),
                },
                "urgency": {
                    "type": "string",
                    "description": "Urgency label: low, normal, high, or urgent. Defaults to normal.",
                },
                "expected_result": {
                    "type": "string",
                    "description": (
                        "What the user should receive when the coworker finishes, such as "
                        "a summary, options, a document, or completion status."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Brief relevant conversation context the coworker needs. Include "
                        "only necessary details; omit unrelated private information."
                    ),
                },
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
        permissions=frozenset({Permission.SYSTEM}),
        side_effect=True,
        timeout_s=1.0,
    )


def _normalize_arguments(arguments: dict) -> dict[str, str]:
    instruction = str(arguments.get("instruction") or "").strip()
    title = str(arguments.get("title") or "").strip()
    if not title:
        title = instruction[:48]
    return {
        "instruction": instruction,
        "title": title,
        "task_type": str(arguments.get("task_type") or "other").strip() or "other",
        "urgency": str(arguments.get("urgency") or "normal").strip() or "normal",
        "expected_result": str(arguments.get("expected_result") or "").strip(),
        "context": str(arguments.get("context") or "").strip(),
    }


def _localized_now(locale: str) -> datetime:
    timezone_name = {
        "zh-CN": "Asia/Shanghai",
        "zh-HK": "Asia/Hong_Kong",
        "zh-TW": "Asia/Taipei",
        "ja-JP": "Asia/Tokyo",
        "en-US": "America/Los_Angeles",
    }.get(locale, "UTC")
    return datetime.now(ZoneInfo(timezone_name))
