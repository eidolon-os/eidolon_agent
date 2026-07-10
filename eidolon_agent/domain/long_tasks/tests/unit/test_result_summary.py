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


class _CapturingLLM:
    model_id = "fake:capture"

    def __init__(self) -> None:
        self.system_prompt = ""

    async def stream(self, messages, **_kwargs):
        from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason

        self.system_prompt = messages[0].content
        yield LLMDelta(text_delta="办好啦～")
        yield LLMDelta(finish=LLMFinishReason.STOP)


class _StubPersonaVoice:
    """Stands in for PersonaVoice.card without spinning up PersonasService."""

    def __init__(self, card) -> None:
        self._card = card

    async def card(self, *, owner_id, companion_id):
        return self._card


async def test_summary_prompt_carries_persona_voice_when_present() -> None:
    from eidolon_agent.domain.personas.voice import PersonaCard

    llm = _CapturingLLM()
    card = PersonaCard(
        name="洁枝",
        archetype="温柔照护者",
        style_hints=("语气温柔",),
    )
    summarizer = LongTaskResultSummarizer(llm, persona_voice=_StubPersonaVoice(card))

    await summarizer.summarize(_record(), "长任务完整结果……")

    # The broadcast is spoken in the companion's voice, not a generic assistant.
    assert "洁枝" in llm.system_prompt
    assert "温柔照护者" in llm.system_prompt
    assert "语音助手的结果摘要器" not in llm.system_prompt


async def test_summary_degrades_to_generic_voice_without_persona() -> None:
    llm = _CapturingLLM()
    summarizer = LongTaskResultSummarizer(llm)  # no persona_voice
    await summarizer.summarize(_record(), "长任务完整结果……")
    # Still works; just no persona preamble.
    assert "洁枝" not in llm.system_prompt
    assert "口吻" in llm.system_prompt


def _record() -> LongTaskRecord:
    session_key = session_key_for("alice", "2026-06-16")
    return LongTaskRecord(
        id="task-1",
        provider="mementos",
        status=LongTaskStatus.SUCCEEDED,
        owner_id="alice",
        companion_id="agent-alice",
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
