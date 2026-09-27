"""Transport end-to-end: real HTTP contract + Agent socket, input-only decision fixtures.

The fake text model and receipt driver are explicit test infrastructure. They
verify orchestration, not live LLM quality, TTS acoustics or physical devices.
"""
import httpx
import pytest
from eidolon_sdk.biz.control.coordination_stream import ROLE_GROUP_STREAM_PATH
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from eidolon_agent.app.admin.authority import SERVICE_TOKEN_ENV
from eidolon_agent.infra.participation import HttpParticipationDecision
from scripts.participation_fixture import fixture_app

from .test_role_group_stream import HEADERS, TOKEN, app, opening, receipt, reply, stops, utterance

pytestmark = pytest.mark.functional


def case(text, kind, action, speaker=None, **kw):
    return dict(user_text=text, trigger_kind=kind, action=action, speaker_role=speaker, **kw)


@pytest.mark.parametrize('text,rules,expected,outcome,budget', [
    ('只请猪八戒回答', [case('只请猪八戒回答', 'user', 'respond', '猪八戒'),
                     case('只请猪八戒回答', 'companion', 'finish', trigger_role='猪八戒')],
     ['b'], 'finished', 8),
    ('各给一个建议', [case('各给一个建议', 'user', 'respond', '孙悟空'),
                    case('各给一个建议', 'companion', 'respond', '猪八戒',
                         trigger_role='孙悟空', trigger_contains='hello'),
                    case('各给一个建议', 'companion', 'finish', trigger_role='猪八戒')],
     ['a', 'b'], 'finished', 8),
    ('这个你来吧', [case('这个你来吧', 'user', 'clarify', '猪八戒',
                       instruction='询问用户指的是哪一位成员，不要猜测。')], ['b'], 'clarification', 8),
    ('先别回答', [case('先别回答', 'user', 'wait')], [], 'waiting', 8),
    ('到此为止', [case('到此为止', 'user', 'finish')], [], 'finished', 8),
    ('没有预设的输入', [], [], 'abstained', 8),
    ('继续补充', [case('继续补充', 'user', 'respond', '孙悟空'),
                 case('继续补充', 'companion', 'respond', '孙悟空', completed_roles=['孙悟空']),
                 case('继续补充', 'companion', 'finish', completed_roles=['孙悟空', '孙悟空'])],
     ['a', 'a'], 'finished', 8),
    ('持续讨论', [case('持续讨论', 'user', 'respond', '猪八戒'),
                 case('持续讨论', 'companion', 'respond', '孙悟空', trigger_role='猪八戒'),
                 case('持续讨论', 'companion', 'respond', '猪八戒', trigger_role='孙悟空')],
     ['b', 'a', 'b'], 'budget_exhausted', 3),
])
def test_semantic_paths_use_http_contract_and_played_context(monkeypatch, text, rules,
                                                           expected, outcome, budget):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    endpoint = HttpParticipationDecision('http://fixture/v1/participation/decide',
                                        transport=httpx.ASGITransport(app=fixture_app(rules)))
    requests = []
    async def decide(request):
        requests.append(request)
        return await endpoint(request)
    instance, turns = app(decision_port=decide)
    with TestClient(instance) as client, client.websocket_connect(
        ROLE_GROUP_STREAM_PATH, headers=HEADERS
    ) as socket:
        opened = opening()
        # Deliberately reverse inventory order: decisions resolve role identities.
        opened['selection']['members'].reverse()
        opened['selection']['reply_budget'] = budget
        opened['selection']['goal'] = '只讨论公开的出游安排'
        socket.send_json(opened)
        assert socket.receive_json()['policy'] == 'semantic-step-v2'
        utterance(socket, text=text)
        stops(socket)
        for index, companion in enumerate(expected):
            end = reply(socket, companion)
            # Neither generated text nor its reply_end triggers next inference.
            assert len(requests) == index + 1
            receipt(socket, end)
        state = socket.receive_json()
        assert state['outcome'] == outcome and state['state'] == 'waiting'
        assert [t.context.companion_id for t in turns] == expected
        assert all(r.user_request.text == text for r in requests)
        assert all(r.scene_goal == '只讨论公开的出游安排' for r in requests)
        for index, request in enumerate(requests):
            assert request.constraints.remaining_replies == budget - index
            assert [m.author_id for m in request.context.recent_messages
                    if m.author_kind == 'companion'] == expected[:index]
        if outcome == 'clarification':
            assert turns[0].action == 'clarify'
            assert '哪一位' in turns[0].instruction
        socket.send_json({'type': 'close'})
        stops(socket)
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 1000


def test_user_answer_after_clarification_keeps_public_context(monkeypatch):
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    endpoint = HttpParticipationDecision('http://fixture/v1/participation/decide',
        transport=httpx.ASGITransport(app=fixture_app([
            case('让他来', 'user', 'clarify', '猪八戒', instruction='询问指的是哪位成员。'),
            case('悟空', 'user', 'respond', '孙悟空'),
            case('悟空', 'companion', 'finish'),
        ])))
    seen = []
    async def decision(request):
        seen.append(request)
        return await endpoint(request)
    instance, turns = app(decision_port=decision)
    with TestClient(instance) as client, client.websocket_connect(
        ROLE_GROUP_STREAM_PATH, headers=HEADERS
    ) as socket:
        socket.send_json(opening())
        socket.receive_json()
        utterance(socket, capture='ambiguous', text='让他来')
        stops(socket)
        receipt(socket, reply(socket, 'b'))
        assert socket.receive_json()['outcome'] == 'clarification'
        utterance(socket, capture='answer', text='悟空')
        stops(socket)
        receipt(socket, reply(socket, 'a'))
        assert socket.receive_json()['outcome'] == 'finished'
        assert [m.author_kind for m in seen[1].context.recent_messages] == [
            'user', 'companion', 'user']
        assert seen[1].context.recent_messages[0].text == '让他来'
        assert [t.action for t in turns] == ['clarify', 'respond']
        socket.send_json({'type': 'close'})
        stops(socket)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


@pytest.mark.parametrize('first_outcome', ['budget', 'decision_error', 'playback_error'])
def test_next_input_continues_same_scene_through_http_contract(monkeypatch, first_outcome):
    """Exercise recovery across the HTTP boundary without reopening the scene."""
    from fastapi.responses import JSONResponse
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    first_text = '继续讨论这个建议'
    second_text = '现在请悟空总结'
    rules = [case(first_text, 'user', 'respond', '猪八戒'),
             case(first_text, 'companion', 'respond', '猪八戒'),
             case(second_text, 'user', 'respond', '孙悟空'),
             case(second_text, 'companion', 'finish')]
    decision_app = fixture_app(rules)

    @decision_app.middleware('http')
    async def unavailable_for_input(request, call_next):
        payload = await request.json()
        if first_outcome == 'decision_error' and payload['user_request']['text'] == first_text:
            return JSONResponse({'error': 'test unavailable'}, status_code=503)
        return await call_next(request)

    endpoint = HttpParticipationDecision('http://fixture/v1/participation/decide',
        transport=httpx.ASGITransport(app=decision_app))
    requests = []
    async def decide(request):
        requests.append(request)
        return await endpoint(request)
    instance, turns = app(decision_port=decide)
    with TestClient(instance) as client, client.websocket_connect(
        ROLE_GROUP_STREAM_PATH, headers=HEADERS
    ) as socket:
        opened = opening()
        opened['selection']['members'].reverse()
        opened['selection']['reply_budget'] = 2
        socket.send_json(opened)
        socket.receive_json()
        utterance(socket, capture='first', text=first_text)
        stops(socket)
        if first_outcome == 'budget':
            for _ in range(2):
                receipt(socket, reply(socket, 'b'))
        elif first_outcome == 'playback_error':
            receipt(socket, reply(socket, 'b'), result='failed')
        while True:
            state = socket.receive_json()
            if state['type'] == 'stop':
                receipt(socket, state)
            else:
                assert state['type'] == 'state'
                break
        assert state['outcome'] == ('budget_exhausted' if first_outcome == 'budget' else 'error')
        if first_outcome == 'decision_error':
            assert state['error_code'] == 'DECISION_HTTP_503'
            assert not turns
        elif first_outcome == 'playback_error':
            assert state['error_code'] == 'TEAM_PLAYBACK_UNCONFIRMED'

        utterance(socket, capture='second', text=second_text)
        stops(socket)
        receipt(socket, reply(socket, 'a'))
        assert socket.receive_json()['outcome'] == 'finished'
        resumed = next(r for r in requests if r.user_request.text == second_text)
        assert resumed.context_ref == opened['selection']['session_id']
        assert resumed.constraints.remaining_replies == 2
        assert resumed.cancellation_epoch > requests[0].cancellation_epoch
        messages = resumed.context.recent_messages
        assert [m.text for m in messages if m.author_kind == 'user'] == [first_text, second_text]
        assert [m.author_id for m in messages if m.author_kind == 'companion'] == (
            ['b', 'b'] if first_outcome == 'budget' else [])
        assert turns[-1].context.companion_id == 'a'
        socket.send_json({'type': 'close'})
        stops(socket)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()
