"""Every route on this app needs the Host's Agent credential.

This surface holds what someone said to their Eidolon and what it was asked to
do, plus the actions that end an Owner's sessions or delete their runtime data.
It had no authentication at all: the boundary was ``bind = "loopback"``, which
protects against the network and not against whatever else runs on the Host.

The test that matters is the enumerating one. A guard each route opts into is a
guard the next route forgets, and the forgotten one will be whichever was added
in a hurry — so this walks the app's own route table rather than a list someone
maintains here.

It also walks the *routers* rather than only the assembled app, because most of
the other tests here mount a router on a bare ``FastAPI()``. If the credential
lived on the app factory, those tests would all be exercising a surface that does
not ship.
"""

from __future__ import annotations

import httpx
import pytest

from eidolon_agent.app.admin.app import build_admin_app
from eidolon_agent.app.admin.authority import SERVICE_TOKEN_ENV, require_service_token
from eidolon_agent.config.settings import Settings

TOKEN = "agent-admin-token"


def _app():
    return build_admin_app(settings=Settings(), agent_registry=None)


def _routes() -> list[tuple[str, str]]:
    """Every (method, path) this app serves, from the app itself."""

    from fastapi.routing import APIRoute

    return sorted(
        (method, route.path)
        for route in _app().routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
        if method not in {"HEAD", "OPTIONS"}
    )


def test_there_are_routes_to_check_so_this_gate_cannot_pass_vacuously() -> None:
    paths = {path for _method, path in _routes()}

    assert len(paths) >= 8, paths
    # The ones that would hurt most if they were ever unguarded.
    assert "/api/admin/conversations/turns/{turn_id}" in paths
    # The one that carries message bodies.
    assert "/api/admin/conversations/{conversation_id}/turns" in paths
    assert "/api/admin/long-tasks/{task_id}/cancel" in paths
    assert "/api/admin/owners/{owner_id}/data" in paths


@pytest.mark.asyncio
async def test_no_route_answers_without_the_credential(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        for method, path in _routes():
            # A path parameter's value is irrelevant: the guard runs before the
            # handler, so an id that exists nowhere still proves the point.
            url = path.format(
                turn_id="t-1",
                task_id="j-1",
                owner_id="owner-1",
                companion_id="c-1",
                conversation_id="conv-1",
                kind="replay",
                filename="x.json",
            )
            answered = await client.request(method, url)

            assert answered.status_code == 401, f"{method} {url} answered {answered.status_code}"


@pytest.mark.asyncio
async def test_a_wrong_credential_is_refused(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        answered = await client.get(
            "/api/admin/conversations/turns",
            headers={"Authorization": "Bearer not-the-token"},
        )

    assert answered.status_code == 401


@pytest.mark.asyncio
async def test_a_host_with_no_credential_says_so_rather_than_refusing_the_caller(
    monkeypatch,
) -> None:
    """503, not 401. "Nobody configured this" and "your token is wrong" are
    different facts, and collapsing them is how the first gets read for weeks as
    the second."""

    monkeypatch.delenv(SERVICE_TOKEN_ENV, raising=False)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        answered = await client.get(
            "/api/admin/conversations/turns",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )

    assert answered.status_code == 503
    assert SERVICE_TOKEN_ENV in answered.text


@pytest.mark.asyncio
async def test_the_schema_is_readable_without_it(monkeypatch) -> None:
    """It describes the shape rather than answering with anyone's data, and a
    consumer that cannot read it cannot check that its client still matches.

    Unguarded by construction rather than by exemption: the docs and the schema
    belong to the app, and the credential belongs to the routers.
    """

    monkeypatch.setenv(SERVICE_TOKEN_ENV, TOKEN)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        for path in ("/api/openapi.json", "/api/docs"):
            answered = await client.get(path)

            assert answered.status_code == 200, path


def test_the_credential_is_carried_by_the_routers_not_the_app() -> None:
    """So a router mounted on a bare app — as most tests here do — is guarded.

    Asserted on the route objects rather than by making a request, because what
    would go wrong is a *new router* declared without the dependency: it would
    pass every request-level test that never thought to call it.
    """

    from fastapi import FastAPI
    from fastapi.routing import APIRoute

    from eidolon_agent.app.admin.routers import (
        chat_test,
        conversations,
        long_tasks,
        owner_runtime,
        reports,
    )

    guard = require_service_token
    for module in (chat_test, conversations, long_tasks, owner_runtime, reports):
        bare = FastAPI()
        bare.include_router(module.router, prefix="/api/admin")
        routes = [route for route in bare.routes if isinstance(route, APIRoute)]
        assert routes, module.__name__
        for route in routes:
            calls = {dependency.call for dependency in route.dependant.dependencies}
            assert guard in calls, f"{module.__name__}: {route.path} is unguarded"
