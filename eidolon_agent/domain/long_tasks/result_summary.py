"""LLM-backed extraction of short TTS-friendly long-task results."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from eidolon_agent.core.ports.llm import LLMPort
from eidolon_agent.core.types.long_task import LongTaskRecord
from eidolon_agent.core.types.messages import ChatMessage, MessageRole

_MAX_SOURCE_CHARS = 12000
_MAX_SUMMARY_CHARS = 120


class LongTaskResultSummarizer:
    """Turn a full worker result into one short sentence suitable for TTS."""

    def __init__(
        self,
        llm: LLMPort,
        *,
        max_source_chars: int = _MAX_SOURCE_CHARS,
        max_summary_chars: int = _MAX_SUMMARY_CHARS,
    ) -> None:
        self._llm = llm
        self._max_source_chars = max_source_chars
        self._max_summary_chars = max_summary_chars

    async def summarize(self, record: LongTaskRecord, result_text: str) -> str | None:
        source = result_text.strip()
        if not source:
            return None
        messages = _messages_for_summary(
            record,
            result_text=_truncate_source(source, self._max_source_chars),
        )
        chunks: list[str] = []
        async for delta in self._llm.stream(
            messages,
            temperature=0.2,
            max_tokens=96,
            request_id=f"long-task-tts-summary-{record.id}-{uuid.uuid4().hex[:8]}",
        ):
            if delta.text_delta:
                chunks.append(delta.text_delta)
            if delta.finish is not None:
                break
        return _clean_summary("".join(chunks), max_chars=self._max_summary_chars)


def _messages_for_summary(
    record: LongTaskRecord,
    *,
    result_text: str,
) -> list[ChatMessage]:
    now = datetime.now(timezone.utc)
    system = (
        "你是语音助手的结果摘要器。请把长任务的完整结果提取成一句适合 TTS 播放的中文短句。"
        "要求：只输出最终播报句；不使用 Markdown、编号、链接、括号说明或多段文本；"
        "不要编造原始结果没有的信息；尽量控制在 60 个汉字以内。"
    )
    user = (
        f"原始任务：{record.task}\n"
        f"期望输出：{record.expected_output or '未提供'}\n"
        f"上下文摘要：{record.context_summary or '未提供'}\n"
        f"完整结果：\n{result_text}"
    )
    return [
        ChatMessage(
            id=uuid.uuid4().hex,
            role=MessageRole.SYSTEM,
            content=system,
            created_at=now,
        ),
        ChatMessage(
            id=uuid.uuid4().hex,
            role=MessageRole.USER,
            content=user,
            created_at=now,
        ),
    ]


def _truncate_source(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[已截断 {omitted} 个字符]"


def _clean_summary(text: str, *, max_chars: int) -> str | None:
    summary = text.strip().strip("\"'“”‘’")
    summary = re.sub(r"^\s*[-*]\s+", "", summary)
    summary = re.sub(r"\s+", " ", summary).strip()
    if not summary:
        return None
    if len(summary) <= max_chars:
        return summary
    trimmed = summary[:max_chars].rstrip(" ，,。；;、")
    return f"{trimmed}。"
