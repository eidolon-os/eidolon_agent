"""FakeLLM — scripted streaming and tool-call sequences."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from eidolon_agent.core.types.llm import LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.infra.llm.providers.fake import FakeLLM

pytestmark = pytest.mark.functional


def _msg(content: str, role: MessageRole = MessageRole.USER) -> ChatMessage:
    return ChatMessage(
        id=uuid.uuid4().hex,
        role=role,
        content=content,
        created_at=datetime.now(timezone.utc),
    )


async def test_default_script_echoes_last_user_message() -> None:
    llm = FakeLLM(per_token_delay_s=0)
    text = ""
    finished = False
    async for d in llm.stream([_msg("你好")], request_id="r"):
        if d.text_delta:
            text += d.text_delta
        if d.finish is LLMFinishReason.STOP:
            finished = True
    assert "你好" in text
    assert "我在这" in text  # default echo suffix
    assert finished


async def test_custom_script_streams_text_and_tool_call() -> None:
    llm = FakeLLM(
        script=[
            {"kind": "text", "text": "hello"},
            {
                "kind": "tool_call",
                "name": "delegate_to_coworker",
                "arguments": {"instruction": "整理资料"},
            },
            {"kind": "text", "text": " world"},
        ],
        per_token_delay_s=0,
    )
    deltas = [d async for d in llm.stream([], request_id="r")]
    text = "".join(d.text_delta or "" for d in deltas)
    assert text == "hello world"
    tcs = [d.tool_call for d in deltas if d.tool_call]
    assert len(tcs) == 1
    assert tcs[0].name == "delegate_to_coworker"
    assert tcs[0].arguments == {"instruction": "整理资料"}
    assert deltas[-1].finish is LLMFinishReason.STOP


async def test_finish_includes_usage() -> None:
    llm = FakeLLM(script=[{"kind": "text", "text": "abc"}], per_token_delay_s=0)
    deltas = [d async for d in llm.stream([_msg("input")], request_id="r")]
    finish = next(d for d in deltas if d.finish is LLMFinishReason.STOP)
    assert finish.usage is not None
    assert finish.usage.tokens_in >= 1
    assert finish.usage.tokens_out >= 1
