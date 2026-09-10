"""Opt-in real socket integration; see scripts/validate_persona_journeys.py."""

import asyncio
import os
from uuid import uuid4

import pytest

from tests.e2e.persona_network_stack import ROOT, TOKEN, persona_stack

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(
        os.environ.get("EIDOLON_PERSONA_NETWORK_E2E") != "1",
        reason="requires loopback sockets and sibling Admin checkout",
    ),
]


@pytest.fixture
async def network(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    async with persona_stack(tmp_path, monkeypatch) as stack:
        yield stack


async def request(stack, method, path, *, status=200, **kwargs):
    response = await stack.http.request(method, stack.base + path, headers=stack.headers, **kwargs)
    assert response.status_code == status, response.text
    return response.json()


async def create(stack, name="小南", persona=None):
    body = {"operation_id": str(uuid4()), "display_name": name}
    if persona is not None:
        body["persona"] = persona
    result = await request(stack, "PUT", "/companions", json=body)
    return result["companion_id"]


def action(snapshot, **fields):
    return {
        "expected_base_genome_id": snapshot["genome_id"],
        "expected_preference_revision": snapshot["preference_revision"],
        "operation_id": str(uuid4()),
        "persona": {},
        **fields,
    }


async def test_create_preview_edit_rename_restore_over_real_http(network):
    s = network
    presets = await request(s, "GET", "/persona-presets")
    draft = {"name": "小南", "persona": presets["presets"][0]["persona"], "text": "今天很累"}
    preview = await request(s, "POST", "/persona-preview", json=draft)
    assert preview["reply"] and not preview["truncated"]
    # A preview must not create a Companion even though it traverses three apps.
    assert (await s.store.companions.list_for_owner("owner-journey")) == []
    companion = await create(s, persona=draft["persona"])
    path = f"/companions/{companion}/persona"
    base = await request(s, "GET", path)
    # The builder fills default internal trait coordinates; authored prose is exact.
    for field, value in draft["persona"].items():
        if field != "traits":
            assert base["persona"][field] == value
    edit = action(base, persona={"voice_portrait": "用短句直接回答", "dialogue_examples": []})
    edited = await request(s, "PUT", path, json=edit)
    assert edited["persona"]["dialogue_examples"] == []
    assert edited["persona"]["character_portrait"] == base["persona"]["character_portrait"]
    named = await request(s, "PUT", path, json=action(edited, action="rename", display_name="小北"))
    assert named["display_name"] == "小北"
    restored = await request(
        s, "PUT", path, json=action(named, action="restore", restore_genome_id=base["genome_id"])
    )
    assert restored["persona"] == base["persona"]
    assert restored["display_name"] == "小北"
    replay = await request(s, "PUT", path, json=edit)
    assert replay == edited  # lost response replay after rename and restore
    assert (await request(s, "GET", path))["genome_id"] == restored["genome_id"]
    timeline = await request(s, "GET", f"/companions/{companion}/persona-history")
    assert len(timeline["chapters"]) == 4
    stale_preview = {**draft, "companion_id": companion, "base_genome_id": base["genome_id"]}
    await request(s, "POST", "/persona-preview", json=stale_preview, status=409)
    s.model.fail = True
    outage = await request(s, "POST", "/persona-preview", json=draft, status=503)
    assert outage["detail"]["retryable"] is True
    # Model outage does not prevent saving personality.
    await request(s, "PUT", path, json=action(restored, persona={"voice_portrait": "仍可保存"}))


async def test_two_clients_conflict_and_replay_without_duplicate_versions(network):
    s = network
    companion = await create(s)
    path = f"/companions/{companion}/persona"
    base = await request(s, "GET", path)
    bodies = [action(base, persona={"voice_portrait": text}) for text in ("A", "B")]
    responses = await asyncio.gather(
        *(s.http.put(s.base + path, json=b, headers=s.headers) for b in bodies)
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    winner = next(i for i, r in enumerate(responses) if r.status_code == 200)
    replay = await request(s, "PUT", path, json=bodies[winner])
    assert replay == responses[winner].json()
    await request(
        s,
        "PUT",
        path,
        json={**bodies[winner], "persona": {"voice_portrait": "reused-id"}},
        status=409,
    )
    timeline = await request(s, "GET", f"/companions/{companion}/persona-history")
    assert len(timeline["chapters"]) == 2


async def test_authentication_and_owner_isolation_at_every_network_boundary(network):
    s = network
    companion = await create(s)
    path = f"/companions/{companion}/persona"
    assert (await s.http.get(s.base + path)).status_code == 401
    assert (
        await s.http.get(
            s.internal_url + "/api/internal/v1/management" + path,
            params={"owner_id": "owner-journey"},
        )
    ).status_code == 401
    assert (await s.http.get(s.data_url + "/api/companion-authority/v1" + path)).status_code == 401
    response = await s.http.get(
        s.internal_url + "/api/internal/v1/management" + path,
        params={"owner_id": "other-owner"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 404
    base = await request(s, "GET", path)
    draft = {
        "name": "小南",
        "persona": {},
        "text": "hi",
        "companion_id": companion,
        "base_genome_id": base["genome_id"],
    }
    response = await s.http.post(
        s.agent_url + "/api/admin/persona/preview",
        json=draft,
        params={"owner_id": "other-owner"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 404
    assert s.model.calls == []


async def test_live_preferences_change_but_existing_conversation_keeps_persona(network):
    from tests.e2e.persona_conversation import Conversation

    s = network
    companion = await create(s, name="原名字")
    path = f"/companions/{companion}/persona"
    initial = await request(s, "GET", path)
    facts = await s.runtime.resolve(owner_id="owner-journey", companion_id=companion)
    old = Conversation(s, facts)
    reply, events = await old.say("你好")
    assert reply and events[-1].data["status"] == "ok"
    updated = await request(
        s, "PUT", path, json=action(initial, action="rename", display_name="新名字")
    )
    preferred = await request(
        s, "PUT", path, json=action(updated, preferences={"response_length": "detailed"})
    )
    await old.say("再说说")
    prompt = str(s.model.calls[-1][0])
    assert "原名字" in prompt and "新名字" not in prompt
    assert "需要解释时提供充分细节" in prompt
    assert (
        old.last_turn.metadata["response_policy"]["preference_revision"]
        == preferred["preference_revision"]
    )
    fresh = Conversation(
        s, await s.runtime.resolve(owner_id="owner-journey", companion_id=companion)
    )
    await fresh.say("你好")
    assert "新名字" in str(s.model.calls[-1][0])
    # Inspect effective per-turn preference trace, not only a matching prompt word.
    assert old.compiler is not fresh.compiler
    assert len(await old.history.recent_window(conversation_id=old.conversation_id)) == 4
    assert len(await fresh.history.recent_window(conversation_id=fresh.conversation_id)) == 2


@pytest.mark.parametrize("condition", ["empty", "kg_only", "unavailable", "timeout"])
async def test_memory_conditions_do_not_break_complete_turn_or_voice_policy(network, condition):
    from tests.e2e.persona_conversation import Conversation, MemoryCondition

    s = network
    companion = await create(s)
    memory = MemoryCondition()
    memory.state = condition
    conversation = Conversation(
        s, await s.runtime.resolve(owner_id="owner-journey", companion_id=companion), memory=memory
    )
    reply, events = await conversation.say("今天有点累")
    assert reply and events[-1].data["status"] == "ok"
    prompt = str(s.model.calls[-1][0])
    assert "1–3" in prompt
    if condition in {"unavailable", "timeout"}:
        assert "不要假装记得" in prompt


async def test_actual_flutter_management_client_over_network(network):
    # The hostile proxy belongs to the server regression. Flutter's own local
    # tester WebSocket must remain reachable during test-runner bootstrap.
    flutter_env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    }
    flutter_env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    process = await asyncio.create_subprocess_exec(
        "flutter",
        "test",
        "--no-pub",
        "--timeout=30s",
        "test/persona_network_e2e_test.dart",
        f"--dart-define=PERSONA_E2E_URL={network.public_url}",
        f"--dart-define=PERSONA_E2E_TOKEN={network.headers['Authorization'].split(' ', 1)[1]}",
        cwd=ROOT / "eidolon_client_mobile",
        env=flutter_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, output.decode()[-5000:]


async def test_recent_conversation_facts_survive_empty_recall_and_remain_bounded(network):
    from tests.e2e.persona_conversation import Conversation, MemoryCondition

    companion = await create(network)
    facts = await network.runtime.resolve(owner_id="owner-journey", companion_id=companion)
    conversation = Conversation(network, facts, memory=MemoryCondition())
    await conversation.say("我的猫叫芝麻，只在这次聊天记得。")
    for index in range(8):
        await conversation.say(f"聊聊今天第{index}件事。")
    await conversation.say("我的猫叫什么？")
    assert "芝麻" in str(network.model.calls[-1][0])
    # The wider window still has an upper bound; it is not whole-session replay.
    for _ in range(2):
        await conversation.say("现在聊聊别的。")
    assert "芝麻" not in str(network.model.calls[-1][0])
