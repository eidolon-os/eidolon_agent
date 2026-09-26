"""Mode boundaries and prompt contracts; not a real-model adherence claim."""

from dataclasses import replace

import pytest
from eidolon_sdk.biz.control.coordination import SceneRole
from eidolon_sdk.biz.participation import Context, Message
from test_coordinated_replies import scope

from eidolon_agent.app.interaction.coordination.role_reply import (
    RoleContextBuilder,
    RoleMember,
    RoleReplyExecutor,
    RoleReplyRequest,
)
from eidolon_agent.core.errors import PermissionDeniedError
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.messages import MessageRole
from eidolon_agent.core.types.tool import ToolCall

pytestmark = pytest.mark.unit


def request(kind="user", companion="a"):
    source = Message(
        message_id="q",
        author_kind=kind,
        author_id="waveshare" if kind == "user" else "b",
        text="悟空和八戒来回讨论水果，各说三句。",
    )
    return RoleReplyRequest(
        scope(companion),
        "group",
        "turn",
        1,
        (RoleMember("a", SceneRole(name="孙悟空")), RoleMember("b", SceneRole(name="猪八戒"))),
        source,
        Context(recent_messages=(source,)),
    )


def test_roles_survive_public_window_without_private_history_or_mutation():
    first = request()
    old_genome = first.scope.runtime.genome.model_dump()
    public = tuple(
        Message(message_id=str(i), author_kind="companion", author_id="b", text=f"公开发言{i}")
        for i in range(30)
    )
    late = replace(first, trigger=public[-1], public_context=Context(recent_messages=public[-16:]))
    messages = RoleContextBuilder().build(late)
    assert '"speaker_role": {"name": "孙悟空"' in messages[0].content
    assert '"companion_id": "b", "role": {"name": "猪八戒"' in messages[0].content
    assert "禁止替其他成员生成台词" in messages[0].content
    assert all(m.role is not MessageRole.USER for m in messages)
    assert "公开发言29" in messages[1].content
    assert "公开发言0" not in messages[1].content
    assert first.scope.runtime.genome.model_dump() == old_genome
    b = RoleContextBuilder().build(replace(late, scope=scope("b")))
    assert '"speaker_role": {"name": "猪八戒"' in b[0].content


@pytest.mark.parametrize(
    "changes",
    [dict(context_ref="foreign"), dict(assignment_revision=2), dict(scope=scope("foreign"))],
)
def test_request_cannot_override_authorized_scene(changes):
    with pytest.raises(PermissionDeniedError):
        replace(request(), **changes)


class Model:
    def __init__(self, deltas):
        self.deltas = deltas
        self.closed = False

    async def stream(self, messages, **kwargs):
        self.messages, self.kwargs = messages, kwargs
        try:
            for delta in self.deltas:
                yield delta
        finally:
            self.closed = True


async def test_uses_shared_llm_policy_and_only_emits_public_speech():
    model = Model([LLMDelta(text_delta="俺老孙选桃子。"), LLMDelta(finish=LLMFinishReason.STOP)])
    events = [e async for e in RoleReplyExecutor(model).run(request())]
    assert events[-1].data["status"] == "ok"
    assert model.kwargs["tools"] == []
    assert model.kwargs["request_id"] == "turn"
    assert model.closed


@pytest.mark.parametrize(
    "deltas",
    [
        [LLMDelta(tool_call=ToolCall(id="x", name="emit_event", arguments={}))],
        [LLMDelta(text_delta="unfinished")],
        [LLMDelta(finish=LLMFinishReason.STOP)],
        [LLMDelta(text_delta="cut"), LLMDelta(finish=LLMFinishReason.LENGTH)],
    ],
)
async def test_invalid_generation_never_completes(deltas):
    model = Model(deltas)
    with pytest.raises((RuntimeError, PermissionDeniedError)):
        _ = [e async for e in RoleReplyExecutor(model).run(request())]
    assert model.closed


async def test_cancel_closes_upstream_without_running_any_companion_pipeline():
    model = Model([LLMDelta(text_delta="一句话"), LLMDelta(finish=LLMFinishReason.STOP)])
    stream = RoleReplyExecutor(model).run(request())
    assert (await anext(stream)).data["text"] == "一句话"
    await stream.aclose()
    assert model.closed
