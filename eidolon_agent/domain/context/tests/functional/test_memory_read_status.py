from dataclasses import replace

import pytest

from eidolon_agent.core.types.memory import MemoryRecallResult
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.context.tests.functional.test_compiler import _locator, _StubPersonas
from eidolon_agent.domain.history import HistoryManager
from tests.helpers import make_turn_input


class Memory:
    def __init__(self, outcome='empty'):
        self.calls = 0
        self.outcome = outcome

    async def recall_context(self, **kwargs):
        self.calls += 1
        if self.outcome == 'error':
            raise RuntimeError('synthetic read failure')
        return MemoryRecallResult(context='memory evidence ' * 50 if self.outcome == 'found' else '', hits=[])


@pytest.mark.parametrize('case,reason', [
    ('disabled', 'personal_memory_disabled'), ('absent', 'provider_absent'),
    ('temporary', 'privacy_policy'), ('no_text', 'no_input'),
])
async def test_skipped_reads_never_claim_attempted(case, reason):
    memory = Memory()
    compiler = ContextCompiler(personas_service=_StubPersonas(), instance_locator=_locator,
                               history_manager=HistoryManager(), memory_port=None if case == 'absent' else memory)
    ti = make_turn_input('' if case == 'no_text' else '以前的计划是什么？')
    if case == 'disabled':
        ti = replace(ti, context=replace(ti.context, memory_enabled=False))
    if case == 'temporary':
        ti.metadata['temporary'] = True
    messages = await compiler.compile(ti)
    trace = ti.metadata['memory_trace']
    assert memory.calls == 0
    assert trace['attempted'] is False and trace['empty'] is False
    assert trace['skipped_reason'] == reason
    assert trace['evidence_status'] == 'not_provided'
    if ti.text:
        assert '"attempted": false' in messages[0].content
        assert reason in messages[0].content


@pytest.mark.parametrize('outcome,status,evidence', [
    ('empty', 'completed', 'no_match'), ('error', 'failed', 'unavailable'),
    ('found', 'completed', 'provided'),
])
async def test_receipt_matches_executed_result(outcome, status, evidence):
    memory = Memory(outcome)
    compiler = ContextCompiler(personas_service=_StubPersonas(), instance_locator=_locator,
                               history_manager=HistoryManager(), memory_port=memory)
    ti = make_turn_input('计划是什么？')
    messages = await compiler.compile(ti)
    trace = ti.metadata['memory_trace']
    assert memory.calls > 0 and trace['attempted'] is True
    assert trace['read_status'] == status
    assert trace['evidence_status'] == evidence
    assert f'"evidence_status": "{evidence}"' in messages[0].content


async def test_found_but_pruned_is_not_no_match():
    compiler = ContextCompiler(personas_service=_StubPersonas(), instance_locator=_locator,
                               history_manager=HistoryManager(), memory_port=Memory('found'))
    ti = make_turn_input('计划是什么？')
    await compiler.compile(ti)
    ledger = ti.metadata['context_ledger']
    payload_cost = sum(x['token_estimate'] for x in ledger['segments'] if x['source'] == 'memory')
    compiler._context_budget_tokens = ledger['total_token_estimate'] - payload_cost
    messages = await compiler.compile(ti)
    trace = ti.metadata['memory_trace']
    assert trace['attempted'] and not trace['empty'] and not trace['context_injected']
    assert trace['evidence_status'] == 'omitted_by_context_budget'
    assert 'memory evidence' not in messages[0].content
    assert '"evidence_status": "omitted_by_context_budget"' in messages[0].content


async def test_timeout_before_port_call_is_not_attempted():
    import asyncio
    memory = Memory()
    compiler = ContextCompiler(personas_service=_StubPersonas(), instance_locator=_locator,
                               history_manager=HistoryManager(), memory_port=memory)
    async def slow_queries(ti):
        await asyncio.sleep(1)
    compiler._memory_recall_queries = slow_queries
    compiler._context_read_timeout_s = 0.01
    ti = make_turn_input('以前说什么？')
    await compiler.compile(ti)
    assert memory.calls == 0
    assert ti.metadata['memory_trace']['attempted'] is False
    assert ti.metadata['memory_trace']['read_status'] == 'failed'
    assert ti.metadata['memory_trace']['evidence_status'] == 'unavailable'
