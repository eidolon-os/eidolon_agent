from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.domain.context.compiler import ContextCompiler, _background_context_block
from eidolon_agent.domain.context.tests.functional.test_compiler import _locator, _StubPersonas
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.domain.history.source import matching_history_refs, speaker_source
from tests.helpers import make_turn_input


def observation(person):
    return SimpleNamespace(
        status="identified", person_id=person, reason="voice_match", display_name=None, profile=None
    )


async def test_binding_uses_retained_history_and_does_not_change_history_prefix():
    history = HistoryManager()
    for person in ("A", "B"):
        await history.append(
            conversation_id="c1",
            message=ChatMessage(
                id=person,
                role=MessageRole.USER,
                content="我喜欢" + person,
                created_at=datetime.now(UTC),
                metadata={"speaker_source": speaker_source(observation(person))},
            ),
        )
    compiler = ContextCompiler(
        personas_service=_StubPersonas(),
        instance_locator=_locator,
        history_manager=history,
        memory_port=None,
    )
    systems = []
    for person in ("A", "B"):
        ti = make_turn_input("我喜欢什么？")
        ti = replace(
            ti,
            context=replace(
                ti.context, speaker_observation=observation(person), memory_enabled=False
            ),
        )
        system = (await compiler.compile(ti))[0].content
        assert '可关联当前识别来源的原话引用：["' + person + '"]' in system
        systems.append(system)
    assert (
        systems[0].split("[CURRENT SPEAKER OBSERVATION]")[0]
        == systems[1].split("[CURRENT SPEAKER OBSERVATION]")[0]
    )
    costs = [
        x["token_estimate"]
        for x in ti.metadata["context_ledger"]["segments"]
        if x["kind"] == "history"
    ]
    compiler._context_budget_tokens = ti.metadata["context_ledger"]["total_token_estimate"] - sum(
        costs
    )
    system = (await compiler.compile(ti))[0].content
    assert 'ref="A"' not in system and 'ref="B"' not in system
    assert '"ref": "A"' not in system and '"ref": "B"' not in system
    assert "可关联当前识别来源的原话引用：[]" in system


def test_no_inheritance_or_assistant_facts_and_no_silent_truncation():
    text = "公共讨论。" * 60 + "这是我哥哥的偏好，不是我的。"
    legacy = ChatMessage(
        id="legacy", role=MessageRole.USER, content=text, created_at=datetime.now(UTC)
    )
    assistant = replace(
        legacy,
        id="answer",
        role=MessageRole.ASSISTANT,
        metadata={"speaker_source": speaker_source(observation("A"))},
    )
    assert matching_history_refs([legacy, assistant], observation("A")) == "[]"
    assert matching_history_refs([legacy], None) == "[]"
    rendered = _background_context_block([legacy, assistant])
    assert "这是我哥哥的偏好，不是我的。" in rendered
    assert '"status": "unrecorded"' in rendered
    assert '"status": "assistant"' in rendered
