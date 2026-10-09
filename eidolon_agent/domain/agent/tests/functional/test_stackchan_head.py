import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from eidolon_sdk.biz.presentation.motion import STACKCHAN_HEAD_TOOL, MotionReceipt, MotionRequest

from eidolon_agent.app.transport.grpc.motion import MotionExchange
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.domain.tools.stackchan_head import StackChanHeadTool
from tests.helpers import make_turn_input


@pytest.mark.asyncio
async def test_receipt_is_correlated_and_execution_waits():
    send = AsyncMock()
    exchange = MotionExchange(send)
    request = MotionRequest(turn_id="turn-1", command_id="motion:1", action={"action": "shake"})
    task = asyncio.create_task(exchange.execute(request))
    await asyncio.sleep(0)
    assert send.await_count == 1 and not task.done()
    assert not exchange.accept(MotionReceipt(command_id="wrong", status="completed"))
    assert exchange.accept(MotionReceipt(command_id="motion:1", status="failed", reason="BUSY"))
    assert (await task).status == "failed"
    assert not exchange.accept(MotionReceipt(command_id="motion:1", status="completed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "rejected", "cancelled"])
async def test_tool_outcome_uses_execution_receipt(status):
    async def execute(request):
        return MotionReceipt(
            command_id=request.command_id,
            status=status,
            completion_basis="software_sequence" if status == "completed" else "unconfirmed",
        )

    tool = StackChanHeadTool(SimpleNamespace(execute=execute))
    result = await tool.invoke(
        ToolCall("call-1", STACKCHAN_HEAD_TOOL, {"action": "shake", "times": 2}),
        ctx=SimpleNamespace(session_id="s", turn_id="t"),
    )
    assert result.ok == (status == "completed")
    assert result.metadata["outcome_state"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile,selected,speculative,allowed,visible",
    [
        ("stackchan.head.v1", True, False, True, True),
        (None, True, False, True, False),
        ("stackchan.head.v1", False, False, True, False),
        ("stackchan.head.v1", True, True, True, False),
        ("stackchan.head.v1", True, False, False, False),
    ],
)
async def test_tool_only_exposed_to_selected_non_speculative_stackchan(
    turn_engine_factory, profile, selected, speculative, allowed, visible
):
    engine = turn_engine_factory()
    ti = replace(make_turn_input("摇头"), motion_executor=SimpleNamespace(execute=AsyncMock()))
    ti.metadata.update(
        motion_profile=profile, selected_outputs={"motion": selected}, speculative=speculative
    )
    schemas, overlay = await engine._tool_schemas(
        ti, CompanionRuntimeConfig(allow_body_control=allowed)
    )
    assert (STACKCHAN_HEAD_TOOL in overlay) is visible
    assert any(s.name == STACKCHAN_HEAD_TOOL for s in schemas) is visible


@pytest.mark.asyncio
async def test_llm_tool_dispatch_executes_shake_then_confirms(turn_engine_factory):
    from eidolon_sdk.biz.presentation import FACE_PROFILE

    from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
    from eidolon_agent.domain.agent.presentation import RESPONSE_TOOL

    executed = []

    async def execute(request):
        executed.append(request)
        return MotionReceipt(
            command_id=request.command_id, status="completed", completion_basis="software_sequence"
        )

    class LLM:
        model_id = "fake:motion"

        async def count_tokens(self, messages):
            return 10

        async def stream(self, messages, **kwargs):
            if not executed:
                assert any(t.name == STACKCHAN_HEAD_TOOL for t in kwargs["tools"])
                yield LLMDelta(
                    tool_call=ToolCall(
                        "head-1", STACKCHAN_HEAD_TOOL, {"action": "shake", "times": 2}
                    ),
                    finish=LLMFinishReason.TOOL_CALLS,
                )
            else:
                yield LLMDelta(
                    tool_call=ToolCall(
                        "reply",
                        RESPONSE_TOOL,
                        {"presentation": {"intent": "confirm", "outcome_ref": "head-1"}},
                    ),
                    finish=LLMFinishReason.TOOL_CALLS,
                )

    ti = replace(make_turn_input("摇头两次"), motion_executor=SimpleNamespace(execute=execute))
    ti.metadata.update(
        motion_profile="stackchan.head.v1",
        selected_outputs={"motion": True},
        presentation_profile=FACE_PROFILE,
    )
    engine = turn_engine_factory(llm=LLM())
    events = [e async for e in engine.run(ti)]
    assert len(executed) == 1 and executed[0].action.times == 2
    assert executed[0].action.action == "shake"
    assert any(e.kind.value == "presentation" for e in events)
    assert not any(e.kind.value == "error" for e in events)
