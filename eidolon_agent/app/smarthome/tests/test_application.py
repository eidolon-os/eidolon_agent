"""Smart-home shares Companion identity, without the companionship runtime."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from eidolon_sdk.biz.smarthome import HomeSessionScope, VoiceResult
from fastapi import FastAPI

from eidolon_agent.app.admin.routers.smarthome import router
from eidolon_agent.app.smarthome.application import SmartHomeApplication
from eidolon_agent.core.errors import DependencyError, NotFoundError, PermissionDeniedError


def authority(owner="owner", companion="companion"):
    port = AsyncMock()
    port.resolve.return_value = SimpleNamespace(
        owner_id=owner, companion_id=companion, runtime_config={},
    )
    return port


def scope():
    return HomeSessionScope(
        owner_id="owner", companion_id="companion", device_ref="panel", session_id="voice",
    )


def application(port):
    command = AsyncMock()
    command.handle.return_value = VoiceResult(
        turn_id="turn", utterance="开灯", outcome="executed", message="灯已打开",
    )
    return SmartHomeApplication(command, interpreter=object(), runtime_authority=port), command


async def test_active_companion_is_checked_every_turn_without_prompt_or_memory_runtime():
    port = authority()
    app, command = application(port)
    result = await app.handle(scope(), "turn", "开灯")
    assert result.outcome == "executed"
    # Revocation cannot be hidden by the existing home conversation context.
    port.resolve.side_effect = NotFoundError("Companion archived")
    with pytest.raises(NotFoundError):
        await app.handle(scope(), "later", "关闭它")
    assert command.handle.await_count == 1
    assert command.handle.call_args.args == ("owner", "panel", "turn", "开灯")


async def test_cross_owner_companion_never_reaches_home_command():
    app, command = application(authority(owner="other"))
    with pytest.raises(PermissionDeniedError):
        await app.handle(scope(), "turn", "开灯")
    command.handle.assert_not_awaited()


async def request_home(monkeypatch, app, body, path="/command"):
    monkeypatch.setenv("EIDOLON_AGENT_ADMIN_API_TOKEN", "test-token")
    host = FastAPI()
    host.state.smart_home_application = app
    host.include_router(router, prefix="/api/admin")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=host), base_url="http://agent",
    ) as client:
        return await client.post(
            "/api/admin/smarthome" + path, json=body,
            headers={"Authorization": "Bearer test-token"},
        )


@pytest.mark.parametrize("missing", ["companion_id", "session_id"])
async def test_old_anonymous_ingress_is_refused(monkeypatch, missing):
    app, command = application(authority())
    body = {**scope().model_dump(), "turn_id": "turn", "utterance": "开灯"}
    del body[missing]
    response = await request_home(monkeypatch, app, body)
    assert response.status_code == 422
    command.handle.assert_not_awaited()


@pytest.mark.parametrize("failure,status", [
    (NotFoundError("inactive"), 403),
    (DependencyError("authority down"), 503),
])
async def test_authority_failure_is_explicit_and_cannot_execute(monkeypatch, failure, status):
    port = authority()
    port.resolve.side_effect = failure
    app, command = application(port)
    response = await request_home(monkeypatch, app, {
        **scope().model_dump(), "turn_id": "turn", "utterance": "开灯",
    })
    assert response.status_code == status
    command.handle.assert_not_awaited()


async def test_transport_and_lifecycle_share_the_same_scope(monkeypatch):
    app, command = application(authority())
    response = await request_home(monkeypatch, app, {
        **scope().model_dump(), "turn_id": "turn", "utterance": "开灯",
    })
    assert response.status_code == 200
    context = command.handle.call_args.kwargs["context"]
    response = await request_home(monkeypatch, app, scope().model_dump(), "/session/end")
    assert response.status_code == 204 and not context.active


async def test_scoped_http_commands_reuse_existing_home_execution(monkeypatch):
    from eidolon_agent.domain.smarthome import SmartHomeCommand
    from eidolon_agent.domain.smarthome.tests.conftest import (
        OWNER,
        FakeDirectory,
        FakeExecutor,
        home_registry,
    )
    from eidolon_agent.infra.interpretation import RulesInterpreter

    directory = FakeDirectory(home_registry(panel="living"))
    executor = FakeExecutor(directory)
    interpreter = RulesInterpreter()
    app = SmartHomeApplication(
        SmartHomeCommand(directory=directory, executor=executor, interpreter=interpreter),
        interpreter=interpreter, runtime_authority=authority(OWNER),
    )
    identity = scope().model_dump() | {"owner_id": OWNER}
    for turn, text in enumerate(["打开客厅窗帘", "关闭客厅窗帘"]):
        response = await request_home(monkeypatch, app, {
            **identity, "turn_id": f"turn-{turn}", "utterance": text,
        })
        assert response.status_code == 200 and response.json()["outcome"] == "executed"
    assert executor.commands == [
        ("living.curtain", "position", "open", {}),
        ("living.curtain", "position", "close", {}),
    ]
