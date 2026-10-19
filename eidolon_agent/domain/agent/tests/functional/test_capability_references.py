"""Reusable capability objects cross turns, but never session or authority boundaries."""
from dataclasses import replace

import pytest

from eidolon_agent.core.types.tool import ToolSchema, ToolResult
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from tests.helpers import make_turn_input


@pytest.mark.asyncio
async def test_tool_references_are_retained_as_scoped_background(turn_engine_factory):
    class Capability:
        schema = ToolSchema("catalogue.lookup", "lookup", {"type": "object", "properties": {}})

        async def invoke(self, call, *, ctx):
            return ToolResult(call.id, call.name, True, content={"items": ["object-42"]}, metadata={
                "outcome_state": "completed", "reference_context": {"candidate_id": "object-42"},
            })

    class RecordingLLM(FakeLLM):
        def __init__(self):
            super().__init__(script=[
                [{"kind": "tool_call", "name": "catalogue.lookup"}],
                [{"kind": "text", "text": "Found an option."}],
            ])
            self.seen = []

        async def stream(self, messages, **kwargs):
            self.seen.append(messages)
            async for delta in super().stream(messages, **kwargs):
                yield delta

    registry = ToolRegistry()
    registry.register(Capability())
    llm = RecordingLLM()
    engine = turn_engine_factory(llm=llm, tool_dispatcher=ToolDispatcher(registry))
    first = make_turn_input("Find an option")
    _ = [event async for event in engine.run(first)]
    tool_messages = [m for m in llm.seen[1] if m.role.value == "tool"]
    assert "outcome_state" in tool_messages[0].content
    history = await engine._history.recent_window(conversation_id=first.conversation_id, window=20)
    assert history[-1].metadata["capability_references"][0]["objects"] == {"candidate_id": "object-42"}
    second = replace(make_turn_input("Use that option"), turn_id="t2", conversation_id=first.conversation_id, session_id=first.session_id)
    _ = [event async for event in engine.run(second)]
    assert any("Capability object references" in m.content and "object-42" in m.content for m in llm.seen[-1])
    other = replace(make_turn_input("Hello"), turn_id="t3", conversation_id=first.conversation_id, session_id="other-session")
    _ = [event async for event in engine.run(other)]
    assert not any("Capability object references" in m.content for m in llm.seen[-1])
