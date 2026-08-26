"""Which of an Owner's Companions this Host is currently running.

The fact this route exists to replace was a guess. Screens showed 「运行中」 when
the Owner had a default Companion — that is, they read a *routing fallback* and
rendered it as *runtime state*, which made "which of my Eidolons is running" a
question with exactly one possible answer no matter what was true.

A Host keeps runtime context per Companion (plan §4.6), so several being live is
the ordinary case. This is that, read from the registry that actually holds it.

Two things it deliberately does not claim, both asserted below:

- it is not presence. Nothing on this Host tracks whether a body is connected,
  so a Companion here is one this Host *can run*, not one somebody can reach;
- absence means "no live runtime" only when the answer arrived. A caller that
  could not ask must say unknown, which is why an unconfigured registry is a
  503 rather than an empty list — an empty list is an answer, and the wrong one.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from eidolon_agent.app.admin.routers import owner_runtime as owner_runtime_router
from eidolon_agent.app.admin.tests.conftest import AUTHORITY_HEADERS
from eidolon_agent.domain.agent.registry import AgentRegistry

pytestmark = pytest.mark.functional


async def _registry(*pairs: tuple[str, str]) -> AgentRegistry:
    async def _factory(instance):
        return object()

    registry = AgentRegistry(instance_factory=_factory)
    for owner_id, companion_id in pairs:
        await registry.resolve_runtime(
            owner_id=owner_id,
            companion_id=companion_id,
            genome_id=f"g-{companion_id}",
        )
    return registry


def _app(registry) -> FastAPI:
    app = FastAPI()
    app.include_router(owner_runtime_router.router, prefix="/api/admin")
    app.state.agent_registry = registry
    return app


async def _get(app: FastAPI, owner_id: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as client:
        return await client.get(
            f"/api/admin/owners/{owner_id}/runtime-companions",
            headers=AUTHORITY_HEADERS,
        )


async def test_more_than_one_can_be_running() -> None:
    """The answer the old screen could not represent."""

    response = await _get(
        _app(await _registry(("owner-1", "c-a"), ("owner-1", "c-b"))), "owner-1"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["owner_id"] == "owner-1"
    assert {row["companion_id"] for row in body["companions"]} == {"c-a", "c-b"}


async def test_it_answers_for_one_owner_only() -> None:
    """Two people on one Host must not appear in each other's list."""

    registry = await _registry(("owner-1", "c-a"), ("owner-2", "c-b"))

    mine = (await _get(_app(registry), "owner-1")).json()
    theirs = (await _get(_app(registry), "owner-2")).json()

    assert [row["companion_id"] for row in mine["companions"]] == ["c-a"]
    assert [row["companion_id"] for row in theirs["companions"]] == ["c-b"]


async def test_an_owner_running_nothing_is_an_empty_answer_not_an_error() -> None:
    """"None of them" is a real state and a screen must be able to show it."""

    response = await _get(_app(await _registry()), "owner-1")

    assert response.status_code == 200, response.text
    assert response.json()["companions"] == []


async def test_each_row_says_when_it_started_and_when_it_was_last_used() -> None:
    """Started alone cannot tell "used a minute ago" from "used at boot".

    Both timestamps travel because a list has to be able to say something
    truthful about recency, and a runtime that was resolved at startup and never
    addressed again is not the same thing as one in use.
    """

    response = await _get(_app(await _registry(("owner-1", "c-a"))), "owner-1")

    row = response.json()["companions"][0]
    assert row["started_at"] and row["last_active_at"]
    assert row["genome_id"] == "g-c-a", "which persona is running, not just which id"


async def test_a_registry_it_cannot_read_is_unknown_rather_than_none() -> None:
    """The distinction the old screen collapsed, in the other direction.

    An empty list is an answer: "nothing of yours is running". If this route
    returned that when it simply could not look, every consumer would render
    "nothing running" for a Host whose Agent was merely unreachable — and would
    be as wrong as the guess this route replaces.
    """

    app = FastAPI()
    app.include_router(owner_runtime_router.router, prefix="/api/admin")
    # Registry deliberately absent.

    response = await _get(app, "owner-1")

    assert response.status_code == 503
    assert "registry" in response.json()["detail"]


async def test_the_read_needs_the_authority_credential() -> None:
    app = _app(await _registry(("owner-1", "c-a")))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    ) as client:
        anonymous = await client.get(
            "/api/admin/owners/owner-1/runtime-companions"
        )

    assert anonymous.status_code in (401, 403)
