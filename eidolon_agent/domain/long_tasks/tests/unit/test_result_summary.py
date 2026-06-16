"""Long-task result TTS summary extraction."""

from __future__ import annotations

import pytest

from eidolon_agent.core.types.long_task import (
    LongTaskRecord,
    LongTaskStatus,
    session_key_for,
    task_key_for,
)
from eidolon_agent.domain.long_tasks import LongTaskResultSummarizer
from eidolon_agent.infra.llm.providers.fake import FakeLLM

pytestmark = pytest.mark.asyncio


async def test_result_summarizer_extracts_short_tts_text() -> None:
    llm = FakeLLM(
        script=[
            {
                "kind": "text",
                "text": "  - 资料整理完成，已生成三条行动建议。  ",
            }
        ],
        per_token_delay_s=0,
    )
    summarizer = LongTaskResultSummarizer(llm)

    summary = await summarizer.summarize(
        _record(),
        "完整结果：1. 已整理资料。2. 生成三条行动建议。",
    )

    assert summary == "资料整理完成，已生成三条行动建议。"


def _record() -> LongTaskRecord:
    session_key = session_key_for("alice", "2026-06-16")
    return LongTaskRecord(
        id="task-1",
        provider="mementos",
        status=LongTaskStatus.SUCCEEDED,
        tenant_id="t",
        user_id="alice",
        conversation_id="c1",
        turn_id="turn-1",
        session_id="s1",
        trace_id="trace-1",
        session_key=session_key,
        task_date="2026-06-16",
        task_key=task_key_for(session_key, "task-1"),
        task="整理资料",
        expected_output="行动建议",
        context_summary="用户希望稍后听结果。",
    )
