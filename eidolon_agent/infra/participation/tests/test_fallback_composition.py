"""The fallback must not override silence, cancellation, or snapshot authority."""
import asyncio

import pytest
from eidolon_sdk.biz.participation import Candidate, DecisionRequest, DecisionResult, Message, Snapshot

from eidolon_agent.app.interaction.coordination.decision import FallbackParticipationDecision
from eidolon_agent.core.ports.participation import DecisionUnavailable
from tests.decision_helpers import decided


def request(timeout_ms=500):
    message = Message(message_id='m', author_kind='user', author_id='u', text='你们聊聊')
    return DecisionRequest(decision_id='d', context_ref='s', context_version=2,
                           membership_revision=3, cancellation_epoch=4, trigger=message,
                           user_request=message, candidates=(Candidate(companion_id='a',
                           display_name='甲', description='伙伴'),), timeout_ms=timeout_ms)


def abstained(req):
    return DecisionResult(**{k: getattr(req, k) for k in Snapshot.model_fields},
                          status='abstained', policy_version='test', model_version='primary')


@pytest.mark.parametrize('action', ['respond', 'clarify', 'wait', 'finish'])
async def test_accepted_primary_never_calls_fallback(action):
    async def primary(req):
        return decided(req, action, 'a' if action in {'respond', 'clarify'} else None,
                       '请澄清对象' if action == 'clarify' else '')

    async def fallback(req):
        pytest.fail('a valid decision must not be reinterpreted')

    result = await FallbackParticipationDecision(primary, fallback, primary_timeout_ms=100)(request())
    assert result.proposal.action == action


@pytest.mark.parametrize('reason', ['abstained', 'DECISION_TIMEOUT', 'DECISION_TRANSPORT_ERROR',
                                     'DECISION_HTTP_429', 'DECISION_HTTP_502',
                                     'DECISION_HTTP_503', 'DECISION_HTTP_504'])
async def test_one_fallback_preserves_input_and_uses_remaining_budget(reason):
    original = request()
    calls = []

    async def primary(req):
        calls.append(('primary', req))
        if reason != 'abstained':
            raise DecisionUnavailable(reason)
        return abstained(req)

    async def fallback(req):
        calls.append(('fallback', req))
        return decided(req, 'respond', 'a')

    await FallbackParticipationDecision(primary, fallback, primary_timeout_ms=100)(original)
    assert [name for name, _ in calls] == ['primary', 'fallback']
    assert calls[0][1].timeout_ms == 100
    assert 0 < calls[1][1].timeout_ms <= original.timeout_ms
    for _, req in calls:
        assert req.model_dump(exclude={'timeout_ms'}) == original.model_dump(exclude={'timeout_ms'})


@pytest.mark.parametrize('code', ['DECISION_INVALID_RESULT', 'DECISION_HTTP_401',
                                  'DECISION_HTTP_403', 'DECISION_HTTP_400',
                                  'DECISION_RESPONSE_TOO_LARGE'])
async def test_contract_and_authority_failures_do_not_fallback(code):
    async def primary(req):
        raise DecisionUnavailable(code)

    async def fallback(req):
        pytest.fail('must preserve the failure')

    with pytest.raises(DecisionUnavailable, match=code):
        await FallbackParticipationDecision(primary, fallback, primary_timeout_ms=100)(request())


@pytest.mark.parametrize('stage', ['primary', 'fallback'])
async def test_stale_result_never_reaches_execution(stage):
    async def primary(req):
        if stage == 'fallback':
            return abstained(req)
        return decided(req, 'respond', 'a').model_copy(update={'cancellation_epoch': 0})

    async def fallback(req):
        assert stage == 'fallback'
        return decided(req, 'respond', 'a').model_copy(update={'context_version': 0})

    with pytest.raises(DecisionUnavailable, match='DECISION_INVALID_RESULT'):
        await FallbackParticipationDecision(primary, fallback, primary_timeout_ms=100)(request())


@pytest.mark.parametrize('stage', ['primary', 'fallback'])
async def test_external_cancellation_propagates_without_another_attempt(stage):
    entered = asyncio.Event()
    stopped = asyncio.Event()
    calls = []

    async def wait():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def primary(req):
        calls.append('primary')
        if stage == 'primary':
            await wait()
        return abstained(req)

    async def fallback(req):
        calls.append('fallback')
        await wait()

    task = asyncio.create_task(FallbackParticipationDecision(primary, fallback,
                              primary_timeout_ms=1000)(request(5000)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert calls == (['primary'] if stage == 'primary' else ['primary', 'fallback'])


async def test_primary_deadline_leaves_time_for_fallback():
    cancelled = asyncio.Event()

    async def primary(req):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def fallback(req):
        assert cancelled.is_set()
        assert 0 < req.timeout_ms < 500
        return decided(req, 'finish')

    result = await FallbackParticipationDecision(primary, fallback, primary_timeout_ms=5)(request())
    assert result.proposal.action == 'finish'


async def test_original_deadline_cancels_fallback_and_does_not_retry():
    stopped = asyncio.Event()
    count = 0

    async def primary(req):
        return abstained(req)

    async def fallback(req):
        nonlocal count
        count += 1
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(DecisionUnavailable, match='DECISION_TIMEOUT'):
        await FallbackParticipationDecision(primary, fallback, primary_timeout_ms=5)(request(30))
    assert count == 1 and stopped.is_set()


async def test_fallback_abstention_is_returned_without_fixed_order_policy():
    async def abstain(req):
        return abstained(req)

    result = await FallbackParticipationDecision(abstain, abstain, primary_timeout_ms=100)(request())
    assert result.status == 'abstained' and result.proposal is None


async def test_exhausted_primary_budget_does_not_start_fallback():
    async def primary(req):
        await asyncio.Event().wait()

    async def fallback(req):
        pytest.fail('no budget left for another model')

    with pytest.raises(DecisionUnavailable, match='DECISION_TIMEOUT'):
        await FallbackParticipationDecision(primary, fallback, primary_timeout_ms=100)(request(5))
