"""delegate_to_coworker — structured delegation contract flows to the record."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.long_task import LongTaskRecord
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools.builtin.submit_long_task import SubmitLongTaskTool

pytestmark = pytest.mark.functional


class _CapturingSubmitter:
    def __init__(self) -> None:
        self.record: LongTaskRecord | None = None

    async def submit(self, record: LongTaskRecord) -> None:
        self.record = record


async def test_delegation_carries_tool_budget_and_duration(caller_ctx) -> None:
    submitter = _CapturingSubmitter()
    tool = SubmitLongTaskTool(long_task_submitter=submitter)

    res = await tool.invoke(
        ToolCall(
            id="c1",
            name="delegate_to_coworker",
            arguments={
                "instruction": "整理最近一周的项目资料",
                "expected_result": "一份结构化清单",
                "tool_budget": 12,
                "expected_duration_hint": "大约一小时",
                "task_type": "document_work",
            },
        ),
        ctx=caller_ctx,
    )

    assert res.ok
    rec = submitter.record
    assert rec is not None
    # Full delegation contract: objective (task), output contract, budget, ETA.
    assert rec.task == "整理最近一周的项目资料"
    assert rec.expected_output == "一份结构化清单"
    assert rec.tool_budget == 12
    assert rec.expected_duration_hint == "大约一小时"
    # Also mirrored into the coworker request payload.
    assert rec.request_payload["tool_budget"] == 12
    assert rec.request_payload["expected_duration_hint"] == "大约一小时"


async def test_delegation_defaults_when_contract_fields_omitted(caller_ctx) -> None:
    submitter = _CapturingSubmitter()
    tool = SubmitLongTaskTool(long_task_submitter=submitter)

    res = await tool.invoke(
        ToolCall(id="c2", name="delegate_to_coworker", arguments={"instruction": "查天气"}),
        ctx=caller_ctx,
    )

    assert res.ok
    assert submitter.record is not None
    # Omitted budget = coworker default (0), no duration hint.
    assert submitter.record.tool_budget == 0
    assert submitter.record.expected_duration_hint == ""


async def test_malformed_tool_budget_does_not_fail_delegation(caller_ctx) -> None:
    submitter = _CapturingSubmitter()
    tool = SubmitLongTaskTool(long_task_submitter=submitter)

    res = await tool.invoke(
        ToolCall(
            id="c3",
            name="delegate_to_coworker",
            arguments={"instruction": "订机票", "tool_budget": "十二"},
        ),
        ctx=caller_ctx,
    )

    assert res.ok
    assert submitter.record is not None
    assert submitter.record.tool_budget == 0  # coerced, not crashed
