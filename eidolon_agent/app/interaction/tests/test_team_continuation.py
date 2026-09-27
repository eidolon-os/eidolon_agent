"""Continuation acceptance at the existing decision port; no runtime policy here."""
import pytest

from eidolon_agent.app.interaction.coordination import PlayedReply, Ports
from eidolon_agent.app.interaction.tests.test_coordination import Harness
from tests.decision_helpers import decided

pytestmark = pytest.mark.unit


async def test_answer_after_clarification_sees_the_actual_public_question():
    h = Harness()
    decisions = []
    async def asr(capture):
        return {'question': 'choose a topic', 'answer': 'the second topic'}[capture]
    async def decide(request):
        decisions.append(request)
        if request.trigger.text == 'choose a topic':
            return decided(request, 'clarify', speaker='b', instruction='Ask which topic the user prefers.')
        if request.trigger.text == 'the second topic':
            return decided(request, speaker='a')
        return decided(request, 'finish')
    async def reply(member, request, permit):
        permit.check()
        return PlayedReply('Which topic do you prefer?' if request.action == 'clarify'
                           else 'Discussing the second topic.', True)
    h.session.ports = Ports(decide, reply, h.stop, asr)
    try:
        h.press('question')
        await h.release('question')
        assert h.session.outcome == 'clarification'
        assert len(decisions) == 1
        h.press('answer')
        await h.release('answer')
        context = decisions[1].context.recent_messages
        assert [m.text for m in context] == [
            'choose a topic', 'Which topic do you prefer?', 'the second topic']
        assert context[1].author_id == 'b'
        assert decisions[1].user_request.text == 'the second topic'
        assert h.session.outcome == 'finished'
    finally:
        await h.session.close()


async def test_budget_yields_and_fresh_input_resets_budget_without_rotating_speakers():
    h = Harness(reply_budget=2)
    remaining = []
    async def decide(request):
        remaining.append(request.constraints.remaining_replies)
        return decided(request, speaker='b')
    h.policy = decide
    try:
        for capture in ('first', 'continue'):
            h.press(capture)
            await h.release(capture)
            assert h.session.outcome == 'budget_exhausted'
            assert h.session.state == 'waiting'
        assert remaining == [2, 1, 2, 1]
        assert [x[1] for x in h.log if x[0] == 'reply'] == ['b'] * 4
        assert [m.author_kind for m in h.session.history].count('user') == 2
    finally:
        await h.session.close()


async def test_failed_playback_does_not_enter_public_history_and_fresh_ptt_recovers():
    h = Harness()
    decisions = []
    async def asr(capture):
        return capture
    async def decide(request):
        decisions.append(request)
        return (decided(request, speaker='a') if request.trigger.author_kind == 'user'
                else decided(request, 'finish'))
    async def reply(member, request, permit):
        return PlayedReply('incomplete text' if request.user_request.text == 'failure'
                           else 'complete reply', request.user_request.text != 'failure')
    h.session.ports = Ports(decide, reply, h.stop, asr)
    try:
        h.press('failure')
        await h.release('failure')
        assert h.session.error_code == 'TEAM_PLAYBACK_UNCONFIRMED'
        assert [m.text for m in h.session.history] == ['failure']
        h.press('retry')
        await h.release('retry')
        assert h.session.outcome == 'finished' and not h.session.error_code
        assert not h.session.failures
        assert [m.text for m in h.session.history] == ['failure', 'retry', 'complete reply']
        assert all(m.text != 'incomplete text' for req in decisions
                   for m in req.context.recent_messages)
    finally:
        await h.session.close()
