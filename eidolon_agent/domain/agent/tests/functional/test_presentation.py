import pytest
from eidolon_sdk.biz.presentation import FACE_PROFILE

from eidolon_agent.app.transport.grpc.codec import turn_event_to_proto
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.tool import ToolCall, ToolResult
from eidolon_agent.domain.agent.presentation import RESPONSE_TOOL, validate_response
from tests.helpers import make_turn_input


class ResponseLLM:
    model_id = "fake:presentation"

    def __init__(self, intent="acknowledge", raw=False, public_text=None):
        self.public_text = public_text
        self.intent, self.raw = intent, raw

    async def count_tokens(self, messages):
        return 10

    async def stream(self, messages, **kwargs):
        assert any(t.name == RESPONSE_TOOL for t in kwargs["tools"])
        yield LLMDelta(text_delta="不可流出到客户端的中间文本")
        if self.raw:
            yield LLMDelta(finish=LLMFinishReason.STOP)
        else:
            yield LLMDelta(
                tool_call=ToolCall(
                    "response",
                    RESPONSE_TOOL,
                    {"presentation": {"intent": self.intent}, "public_text": self.public_text},
                ),
                finish=LLMFinishReason.TOOL_CALLS,
            )


@pytest.mark.asyncio
async def test_structured_response_is_typed_and_no_preamble_escapes(turn_engine_factory):
    engine = turn_engine_factory(llm=ResponseLLM())
    ti = make_turn_input("你好")
    ti.metadata["presentation_profile"] = FACE_PROFILE
    events = [e async for e in engine.run(ti)]
    presentations = [e for e in events if e.kind.value == "presentation"]
    assert len(presentations) == 1 and events[-1].kind.value == "done"
    assert not [e for e in events if e.kind.value in {"delta", "tool_call"}]
    proto = turn_event_to_proto(presentations[0])
    assert proto.presentation.intent == "acknowledge"
    assert proto.presentation.turn_id == ti.turn_id and proto.presentation.schema_version == 1
    assert ti.metadata["presentation_delivery"] == "unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,raw", [("confirm", False), ("acknowledge", True)])
async def test_unproven_success_or_unstructured_output_is_not_presented(
    turn_engine_factory, intent, raw
):
    engine = turn_engine_factory(llm=ResponseLLM(intent, raw))
    ti = make_turn_input("记住了吗")
    ti.metadata["presentation_profile"] = FACE_PROFILE
    events = [e async for e in engine.run(ti)]
    assert any(e.kind.value == "error" for e in events)
    assert not any(e.kind.value in {"presentation", "delta"} for e in events)


@pytest.mark.parametrize("ok,state", [(False, "completed"), (True, "accepted"), (True, None)])
def test_only_actual_completed_outcomes_can_confirm(ok, state):
    candidate = {"presentation": {"intent": "confirm", "outcome_ref": "tool-1"}}
    result = ToolResult("tool-1", "example", ok, metadata={"outcome_state": state})
    with pytest.raises(ValueError):
        validate_response(candidate, turn_id="t", session_id="s", outcomes={"tool-1": result})


def test_completed_outcome_is_scoped_to_this_turn():
    candidate = {"presentation": {"intent": "confirm", "outcome_ref": "tool-1"}}
    result = ToolResult("tool-1", "example", True, metadata={"outcome_state": "completed"})
    _, intent = validate_response(
        candidate, turn_id="t", session_id="s", outcomes={"tool-1": result}
    )
    assert intent.outcome_ref == "tool-1"
    with pytest.raises(ValueError):
        validate_response(candidate, turn_id="next", session_id="s", outcomes={})


@pytest.mark.asyncio
async def test_history_waits_for_terminal_device_feedback(turn_engine_factory):
    from eidolon_sdk.biz.presentation import PresentationReceipt

    from eidolon_agent.core.types.presentation import PresentationFeedback

    engine = turn_engine_factory(llm=ResponseLLM())
    ti = make_turn_input("你好")
    ti.metadata["presentation_profile"] = FACE_PROFILE
    ti = __import__("dataclasses").replace(ti, presentation_feedback=PresentationFeedback())
    events = []
    async for event in engine.run(ti):
        events.append(event)
        if event.kind.value == "presentation":
            assert ti.metadata["presentation_delivery"] == "unconfirmed"
            wrong = PresentationReceipt(
                presentation_id=f"face:{ti.turn_id}",
                response_id="wrong",
                status="completed",
                sequence=3,
            )
            assert not ti.presentation_feedback.accept(wrong)
            good = wrong.model_copy(update={"response_id": f"response:{ti.turn_id}"})
            assert ti.presentation_feedback.accept(good)
            assert not ti.presentation_feedback.accept(good)
    assert events[-1].kind.value == "done"
    assert ti.metadata["presentation_delivery"] == "completed"
    assert ti.metadata["presentation_receipt"]["sequence"] == 3


@pytest.mark.asyncio
async def test_language_and_generation_done_do_not_wait_for_expression(turn_engine_factory):
    import asyncio
    from dataclasses import replace

    from eidolon_sdk.biz.presentation import PresentationReceipt

    from eidolon_agent.core.types.presentation import PresentationFeedback

    engine = turn_engine_factory(llm=ResponseLLM(public_text="你好，我在这里。"))
    ti = replace(make_turn_input("你好"), presentation_feedback=PresentationFeedback())
    ti.metadata.update(
        presentation_profile=FACE_PROFILE, selected_outputs={"speech": True, "expression": True}
    )
    events = []
    stream = engine.run(ti)
    while True:
        event = await asyncio.wait_for(anext(stream), timeout=0.5)
        events.append(event)
        if event.kind.value == "done":
            break
    assert [e.data["text"] for e in events if e.kind.value == "delta"] == ["你好，我在这里。"]
    assert ti.metadata["presentation_delivery"] == "unconfirmed"
    # Failure evidence arrives after DONE and remains distinct from language.
    ti.presentation_feedback.accept(
        PresentationReceipt(
            presentation_id=f"face:{ti.turn_id}",
            response_id=f"response:{ti.turn_id}",
            status="rejected",
            sequence=1,
            reason="COMMAND_CLOCK_UNAVAILABLE",
        )
    )
    assert [e async for e in stream] == []
    assert ti.metadata["presentation_delivery"] == "rejected"
    assert ti.metadata["language_delivery"] == "unconfirmed"


def test_response_schema_distinguishes_mixed_from_strict_silent():
    from eidolon_sdk.biz.presentation import OutputSelection

    from eidolon_agent.domain.agent.presentation import response_schema

    mixed = OutputSelection(speech=True, expression=True)
    silent = OutputSelection(expression=True)
    assert (
        response_schema(mixed).json_schema["properties"]["public_text"]["anyOf"][0]["type"]
        == "string"
    )
    assert response_schema(silent).json_schema["properties"]["public_text"]["type"] == "null"
    with pytest.raises(ValueError, match="PUBLIC_ANSWER_REQUIRED"):
        validate_response(
            {"presentation": {"intent": "acknowledge"}},
            turn_id="t",
            session_id="s",
            outcomes={},
            outputs=mixed,
        )
    candidate, _ = validate_response(
        {"presentation": {"intent": "acknowledge"}, "public_text": "不得存成说过的话"},
        turn_id="t",
        session_id="s",
        outcomes={},
        outputs=silent,
    )
    assert candidate.public_text is None


def test_explicit_suppression_remains_allowed_in_voice_session():
    from eidolon_sdk.biz.presentation import OutputSelection

    candidate, intent = validate_response(
        {"presentation": {"intent": "none"}, "public_text": None},
        turn_id="t",
        session_id="s",
        outcomes={},
        outputs=OutputSelection(speech=True, expression=True),
    )
    assert candidate.public_text is None and intent.intent == "none"
