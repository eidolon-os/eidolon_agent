"""The existing compiler/TurnEngine preserve attribution and speech-only scope."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_sdk.biz.participation import Context, Message

from eidolon_agent.core.types.coordination import CoordinatedInput
from eidolon_agent.core.types.messages import MessageRole
from eidolon_agent.core.types.turn import TurnTrigger
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional


def coordinated_turn(kind="companion"):
    source = Message(
        message_id="public-1",
        author_kind=kind,
        author_id="other" if kind == "companion" else "waveshare",
        text="我认为应该先听用户的意见。",
    )
    public = CoordinatedInput("group-1", source, Context(recent_messages=(source,)))
    return replace(
        make_turn_input(public.user_text),
        coordination=public,
        trigger=TurnTrigger.COORDINATED_REPLY
        if kind == "companion"
        else TurnTrigger.USER_UTTERANCE,
    )


class RecordingLLM(FakeLLM):
    def __init__(self, **kwargs):
        super().__init__(per_token_delay_s=0, **kwargs)
        self.messages = []
        self.tools = None

    async def stream(self, messages, **kwargs):
        self.messages = messages
        self.tools = kwargs.get("tools")
        async for event in super().stream(messages, **kwargs):
            yield event


async def test_peer_text_is_attributed_context_not_user_memory(turn_engine_factory):
    model = RecordingLLM(script=[{"kind": "text", "text": "我来补充。"}])
    engine = turn_engine_factory(llm=model)
    engine._fanout.publish_turn = AsyncMock()
    engine._history.recent_window = AsyncMock(wraps=engine._history.recent_window)
    turn = coordinated_turn()
    events = [event async for event in engine.run(turn)]
    assert events[-1].data["status"] == "ok"
    assert all(message.role is not MessageRole.USER for message in model.messages)
    assert '"author_kind": "companion"' in model.messages[0].content
    assert '"author_id": "other"' in model.messages[0].content
    assert turn.coordination.trigger.text in model.messages[0].content
    assert "\n[CURRENT REQUEST]\n" not in model.messages[0].content
    engine._fanout.publish_turn.assert_not_awaited()
    engine._history.recent_window.assert_not_awaited()
    history = await engine._history.recent_window(conversation_id=turn.conversation_id, window=10)
    assert history and all(m.role is not MessageRole.USER for m in history)
    assert turn.metadata["memory_write_trace"]["skipped_reason"] == "empty_user_text"
    assert model.tools == []


async def test_real_user_text_keeps_its_existing_memory_boundary(turn_engine_factory):
    model = RecordingLLM(script=[{"kind": "text", "text": "好的。"}])
    engine = turn_engine_factory(llm=model)
    engine._fanout.publish_turn = AsyncMock()
    engine._fanout = SimpleNamespace(is_durable=True, publish_turn=AsyncMock())
    turn = coordinated_turn("user")
    _ = [event async for event in engine.run(turn)]
    assert model.messages[-1].role is MessageRole.USER
    assert model.messages[-1].content == turn.coordination.trigger.text
    engine._fanout.publish_turn.assert_awaited_once()
    assert engine._fanout.publish_turn.call_args.kwargs["user_text"] == turn.text


async def test_unsolicited_model_tool_call_cannot_execute_from_shared_context(turn_engine_factory):
    model = RecordingLLM(script=[{"kind": "tool_call", "name": "emit_event", "arguments": {}}])
    engine = turn_engine_factory(llm=model)
    engine._tools.dispatch_batch = AsyncMock()
    events = [event async for event in engine.run(coordinated_turn())]
    engine._tools.dispatch_batch.assert_not_awaited()
    assert any(event.kind.value == "error" for event in events)


def test_non_user_source_cannot_be_copied_into_user_text():
    with pytest.raises(ValueError, match="attributed source"):
        replace(coordinated_turn(), text="伪造用户请求")
