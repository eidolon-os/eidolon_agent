"""Admin FastAPI application factory.

Mounts all admin routers under ``/api/admin`` and exposes OpenAPI docs at
``/api/docs``.

This app is independent of the core transport layer and can be mounted on its
own uvicorn instance or composed into a larger ASGI app.

Every router it mounts carries this Host's Agent credential requirement — on the
router rather than here, so the routes are guarded in the tests that mount them
directly too. See ``admin/authority.py`` for why, and for why liveness stopped
being a conversation read.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from eidolon_agent.app.admin.routers import (
    chat_test,
    conversations,
    long_tasks,
    owner_runtime,
    persona_preview,
    reports,
    role_groups,
    smarthome,
)
from eidolon_agent.app.interaction.coordination.application import IpTeamApplication
from eidolon_agent.config.settings import Settings
from eidolon_agent.infra.participation import HttpParticipationDecision
from eidolon_agent.infra.participation.llm import LlmParticipationDecision
from eidolon_agent.app.interaction.coordination.decision import FallbackParticipationDecision


def build_admin_app(
    *,
    settings: Settings,
    agent_registry,
    personas_service=None,
    revocation_kv=None,
    runtime_authority=None,
    runtime_store=None,
    long_task_submitter=None,
    memory_routes=None,
    memory_discovery_refresher=None,
    live_turns=None,
    llm_router=None,
    participation_decision=None,
    smart_home_application=None,
) -> FastAPI:
    if settings.participation.llm_fallback_enabled and participation_decision is None:
        if not settings.participation.url or llm_router is None or llm_router.model_id == "fake":
            raise ValueError("participation fallback requires a configured primary and real LLM")
    owned_decision = (
        HttpParticipationDecision(settings.participation.url, token=settings.participation.token)
        if participation_decision is None and settings.participation.url else None
    )

    decision = participation_decision if participation_decision is not None else owned_decision
    if settings.participation.llm_fallback_enabled and participation_decision is None:
        decision = FallbackParticipationDecision(
            owned_decision,
            LlmParticipationDecision(llm_router, timeout_ms=settings.participation.fallback_timeout_ms),
            primary_timeout_ms=settings.participation.primary_timeout_ms,
        )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if owned_decision is not None:
                await owned_decision.aclose()

    app = FastAPI(
        lifespan=lifespan,
        title="eidolon-agent admin",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.http.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    app.state.role_group_connections = role_groups.SceneConnections()
    app.state.ip_team_application = IpTeamApplication(
        llm=llm_router, runtime_authority=runtime_authority,
        decide=decision,
    )
    app.state.settings = settings
    app.state.agent_registry = agent_registry
    app.state.personas_service = personas_service
    app.state.llm_router = llm_router
    # Phase 33.B1: expose the DEVICE_REVOCATIONS KV so the admin
    # /users/{id}/revoke-sessions route can write user-level
    # revocation keys. Verifier reads via its own ``revocation_kv``
    # already configured at bootstrap (same instance).
    app.state.revocation_kv = revocation_kv
    app.state.runtime_authority = runtime_authority
    app.state.runtime_store = runtime_store
    # The same worker the delegate tool submits to. Retry hands the task back to
    # it rather than editing a row: nothing polls the store for accepted tasks,
    # so a retry that only wrote ``accepted`` never ran.
    app.state.long_task_submitter = long_task_submitter
    app.state.memory_routes = memory_routes
    app.state.memory_discovery_refresher = memory_discovery_refresher
    # The turns this process is running right now. Absent when nothing observes
    # them, which is why every read of it is optional: the durable rows are the
    # answer either way, and a live turn is an addition to them.
    app.state.live_turns = live_turns
    app.state.smart_home_application = smart_home_application

    app.include_router(
        owner_runtime.router,
        prefix="/api/admin",
        tags=["owner-runtime"],
    )
    app.include_router(role_groups.router, prefix="/api/admin", tags=["role-groups"])
    app.include_router(chat_test.router, prefix="/api/admin", tags=["chat-test"])
    app.include_router(persona_preview.router, prefix="/api/admin", tags=["persona-preview"])
    app.include_router(conversations.router, prefix="/api/admin", tags=["conversations"])
    app.include_router(long_tasks.router, prefix="/api/admin", tags=["long-tasks"])
    app.include_router(reports.router, prefix="/api/admin", tags=["reports"])
    app.include_router(smarthome.router, prefix="/api/admin", tags=["smarthome"])
    return app
