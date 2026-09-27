import httpx
import pytest
from eidolon_sdk.biz.participation import Candidate, DecisionRequest, Message

from eidolon_agent.core.ports.participation import DecisionUnavailable
from eidolon_agent.infra.participation import HttpParticipationDecision
from tests.decision_helpers import decided

pytestmark = pytest.mark.unit


def request():
    message = Message(message_id='user', author_kind='user', author_id='input', text='你好')
    return DecisionRequest(decision_id='d', context_ref='s', context_version=1,
                           membership_revision=1, cancellation_epoch=1, trigger=message,
                           user_request=message, candidates=(Candidate(companion_id='a',
                           display_name='甲', description='角色甲'),), timeout_ms=100)


@pytest.mark.parametrize('failure,code', [
    ('foreign', 'DECISION_INVALID_RESULT'), ('stale', 'DECISION_INVALID_RESULT'),
    ('broken', 'DECISION_INVALID_RESULT'), ('oversize', 'DECISION_RESPONSE_TOO_LARGE'),
    ('status', 'DECISION_HTTP_503'), ('timeout', 'DECISION_TIMEOUT'),
])
async def test_bad_endpoint_never_becomes_a_reply(failure, code):
    req = request()
    async def handler(wire):
        assert wire.headers['authorization'] == 'Bearer local-test'
        if failure == 'timeout':
            raise httpx.ReadTimeout('timeout')
        if failure == 'status':
            return httpx.Response(503)
        if failure == 'broken':
            return httpx.Response(200, content=b'not-json')
        if failure == 'oversize':
            return httpx.Response(200, content=b'x' * 262145)
        result = decided(req, speaker='foreign' if failure == 'foreign' else 'a')
        if failure == 'stale':
            result = result.model_copy(update={'cancellation_epoch': 0})
        return httpx.Response(200, json=result.model_dump(mode='json'))
    port = HttpParticipationDecision('http://fixture/decide', token='local-test',
                                    transport=httpx.MockTransport(handler))
    with pytest.raises(DecisionUnavailable, match=code):
        await port(req)


async def test_fixture_resolves_dynamic_ids_and_abstains_on_ambiguous_names():
    from scripts.participation_fixture import InputMatchedDecisions
    fixture = InputMatchedDecisions([dict(user_text='你好', trigger_kind='user',
                                         action='respond', speaker_role='甲')])
    req = request()
    # The chosen identity is read from this request, not a fixture's member order.
    candidate = req.candidates[0].model_copy(update={'companion_id': 'new-id'})
    response = await fixture(req.model_copy(update={'candidates': (candidate,)}))
    assert response.proposal.participants == ('new-id',)
    response = await fixture(req.model_copy(update={'candidates': (candidate, req.candidates[0])}))
    assert response.status == 'abstained'
    response = await InputMatchedDecisions([fixture.cases[0], fixture.cases[0]])(req)
    assert response.status == 'abstained'
