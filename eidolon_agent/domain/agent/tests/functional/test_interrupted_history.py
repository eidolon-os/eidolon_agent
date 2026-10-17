"""Cancellation continuity through the real engine, compiler and SQLite store."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from eidolon_sdk.biz.dialogue_control import CommittedTurnDecision, TurnCommitBoundary

from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.infra.persistence.agent_runtime import build_agent_history_hydrator
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore
from tests.helpers import make_turn_input

pytestmark = pytest.mark.functional


class _PausingLLM:
    model_id = "history-probe"

    def __init__(self, prefix: str, pauses: int = 2):
        self.prefix = prefix
        self.ready = [asyncio.Event() for _ in range(pauses)]
        self.prompts = []

    async def stream(self, messages, **_kwargs):
        call = len(self.prompts)
        self.prompts.append(messages)
        if call < len(self.ready):
            if self.prefix:
                yield LLMDelta(text_delta=self.prefix)
            self.ready[call].set()
            await asyncio.Event().wait()
        yield LLMDelta(text_delta="收到补充。")
        yield LLMDelta(finish=LLMFinishReason.STOP)


def _voice_turn(text: str, turn_id: str):
    ti = replace(make_turn_input(text), turn_id=turn_id, input_modality="voice")
    ti.metadata["turn_decision"] = CommittedTurnDecision.create(
        text=text, boundary=TurnCommitBoundary.FRAMEWORK_COMPLETED, eot_score=0.8
    ).as_metadata()
    return ti


async def _drive(engine, ti):
    return [event async for event in engine.run(ti)]


@pytest.mark.parametrize("seed_count", [0, 20])
@pytest.mark.parametrize("prefix,played_chars", [
    ("", None), ("先听我说，后面还很长。", None),
    ("先听我说，后面还很长。", 0), ("先听我说，后面还很长。", 4),
])
async def test_immediate_continuation_does_not_wait_for_cancelled_turn_storage(
    turn_engine_factory, tmp_path, seed_count, prefix, played_chars,
):
    store = AgentRuntimeStore.open(tmp_path / "history.sqlite3")
    await store.init_schema()
    llm = _PausingLLM(prefix)
    engine = turn_engine_factory(llm=llm, runtime_store=store)
    release_old_writes = asyncio.Event()
    latest_written = asyncio.Event()
    persist = engine._turn_persister

    async def delayed_persist(**kwargs):
        if kwargs["ti"].turn_id != "last":
            await release_old_writes.wait()
        await persist(**kwargs)
        if kwargs["ti"].turn_id == "last":
            latest_written.set()

    engine._turn_persister = delayed_persist
    tasks = []
    texts = ["今天想讨论一下那个方案。", "就是上次说的实时语音方案。", "先谈第二部分。"]
    try:
        for i in range(seed_count):
            await engine._history.append(conversation_id="c1", message=ChatMessage(
                id=f"seed-{i}", role=MessageRole.USER, content=f"早先的消息 {i}",
                created_at=datetime.now(UTC) - timedelta(minutes=1),
            ))
        for i, text in enumerate(texts[:2]):
            ti = _voice_turn(text, f"old-{i}")
            if played_chars is not None:
                ti.metadata["cancel_played_chars"] = played_chars
            task = asyncio.create_task(_drive(engine, ti))
            tasks.append(task)
            await asyncio.wait_for(llm.ready[i].wait(), 2)
            task.cancel()
            # Deliberately start the next turn without awaiting old cleanup.
        await asyncio.wait_for(_drive(engine, _voice_turn(texts[2], "last")), 2)
        for task in tasks:
            with pytest.raises(asyncio.CancelledError):
                await task

        for i, prompt in enumerate(llm.prompts):
            history_lines = [line for line in prompt[0].content.splitlines() if ". role=" in line]
            assert not any(texts[i] in line for line in history_lines)
            assert prompt[-1].role is MessageRole.USER
            assert prompt[-1].content == texts[i]
            for prior in texts[:i]:
                matching = [line for line in history_lines if prior in line]
                assert len(matching) == 1
                assert "status=interrupted" in matching[0]
            heard = prefix if played_chars is None else prefix[:played_chars]
            prior_answers = [line for line in history_lines if "role=assistant" in line]
            assert len(prior_answers) == (i if heard else 0)
            assert all(line.endswith(heard) for line in prior_answers)

        # The third turn commits before either old turn. Cold reads must still
        # choose the latest dialogue, not the last SQLite insertion.
        await asyncio.wait_for(latest_written.wait(), 2)
        release_old_writes.set()
        await engine._background.drain(timeout_s=2)
        cold = HistoryManager(hydrate_messages=build_agent_history_hydrator(store))
        for history in (engine._history, cold):
            messages = await history.recent_window(conversation_id="c1", window=20)
            users = [m for m in messages if m.role is MessageRole.USER and m.content in texts]
            assert [m.content for m in users] == texts
            assert [m.metadata["turn_status"] for m in users] == ["cancelled", "cancelled", "ok"]
            latest = await history.recent_window(conversation_id="c1", window=2)
            assert [m.content for m in latest] == [texts[2], "收到补充。"]
    finally:
        release_old_writes.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine._background.drain(timeout_s=2)
        await store.close()


@pytest.mark.parametrize("metadata", [
    {"speculative": True}, {"private": True}, {"temporary": True}, {"turn_decision": None},
])
async def test_cancelled_input_respects_existing_history_boundaries(turn_engine_factory, metadata):
    llm = _PausingLLM("", pauses=1)
    engine = turn_engine_factory(llm=llm)
    ti = _voice_turn("这段不应进入公开上下文。", "private")
    ti.metadata.update(metadata)
    task = asyncio.create_task(_drive(engine, ti))
    await asyncio.wait_for(llm.ready[0].wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _drive(engine, _voice_turn("接着聊。", "public"))
    assert ti.text not in llm.prompts[1][0].content
    await engine._background.drain(timeout_s=1)


async def test_input_is_retained_when_cancelled_during_context_compilation(turn_engine_factory):
    engine = turn_engine_factory()
    compile_started = asyncio.Event()
    compile_context = engine._compiler.compile

    async def blocked_compile(ti):
        compile_started.set()
        await asyncio.Event().wait()

    engine._compiler.compile = blocked_compile
    ti = _voice_turn("尚未开始生成的前半句。", "before-model")
    task = asyncio.create_task(_drive(engine, ti))
    await asyncio.wait_for(compile_started.wait(), 2)
    # Input is already visible even before the cancellation handler runs.
    recent = await engine._history.recent_window(conversation_id="c1")
    assert [m.content for m in recent] == [ti.text]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    engine._compiler.compile = compile_context
    prompt = await compile_context(_voice_turn("继续补充。", "next"))
    assert ti.text in prompt[0].content
    assert "status=interrupted" in prompt[0].content
    await engine._background.drain(timeout_s=1)
