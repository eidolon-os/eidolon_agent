"""Drafts use the production compiler without runtime or memory writes."""

from eidolon_sdk.biz.persona import ConversationPreferences, PersonaAuthoring, PersonaPreviewRequest

from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.domain.personas.preview import preview_persona


class Model:
    async def stream(self, messages, **kwargs):
        self.messages = messages
        self.kwargs = kwargs
        yield LLMDelta(text_delta="我在。")
        yield LLMDelta(finish=LLMFinishReason.STOP)


async def test_preview_compiles_actual_draft_and_changes_digest_with_preferences():
    draft = PersonaPreviewRequest(
        name="小南", persona=PersonaAuthoring(character_portrait="PREVIEW_MARKER"), text="今天累了"
    )
    model = Model()
    first = await preview_persona(draft, llm=model)
    assert first.reply == "我在。"
    assert not first.truncated
    assert model.kwargs["tools"] == []
    prompt = str(model.messages)
    assert "PREVIEW_MARKER" in prompt
    assert "小南" in prompt
    second = await preview_persona(
        draft.model_copy(
            update={"preferences": ConversationPreferences(response_length="detailed")}
        ),
        llm=model,
    )
    assert first.draft_digest != second.draft_digest


async def test_preview_has_no_previous_conversation():
    model = Model()
    draft = PersonaPreviewRequest(name="南", persona=PersonaAuthoring(), text="FIRST_UNIQUE")
    await preview_persona(draft, llm=model)
    await preview_persona(draft.model_copy(update={"text": "SECOND_UNIQUE"}), llm=model)
    assert "FIRST_UNIQUE" not in str(model.messages)
    assert "SECOND_UNIQUE" in str(model.messages)


async def test_preview_marks_truncation():
    class Truncated:
        async def stream(self, *args, **kwargs):
            yield LLMDelta(text_delta="partial", finish=LLMFinishReason.LENGTH)

    result = await preview_persona(
        PersonaPreviewRequest(name="南", persona=PersonaAuthoring(), text="你好"), llm=Truncated()
    )
    assert result.truncated
