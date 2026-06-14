"""Admin FastAPI application factory.

Mounts all admin routers under ``/api/admin`` and exposes OpenAPI docs at
``/api/docs``.

This app is independent of the core transport layer and can be mounted on its
own uvicorn instance or composed into a larger ASGI app.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from eidolon_agent.app.admin.routers import (
    chat_test,
    conversations,
    devices,
    long_tasks,
    personas,
    reports,
    templates,
)
from eidolon_agent.app.admin.routers import pairing as pairing_router
from eidolon_agent.config.settings import Settings


def build_admin_app(
    *,
    settings: Settings,
    agent_registry,
    pairing,
    pairing_verifier=None,
    personas_service=None,
    custom_template_store=None,
    persona_template_registry=None,
    revocation_kv=None,
    session_factory=None,
    memory_routes=None,
    memory_discovery_refresher=None,
) -> FastAPI:
    app = FastAPI(
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

    app.state.settings = settings
    app.state.agent_registry = agent_registry
    app.state.pairing = pairing
    app.state.pairing_verifier = pairing_verifier
    app.state.personas_service = personas_service
    # Phase 33.B1: expose the DEVICE_REVOCATIONS KV so the admin
    # /users/{id}/revoke-sessions route can write user-level
    # revocation keys. Verifier reads via its own ``revocation_kv``
    # already configured at bootstrap (same instance).
    app.state.revocation_kv = revocation_kv
    # Phase 29.D — custom template CRUD. These two are coupled (router
    # mutates the store, then calls registry.refresh_custom() so the
    # in-memory cache stays consistent).
    app.state.custom_template_store = custom_template_store
    app.state.persona_template_registry = persona_template_registry
    # Phase 34.A: conversations router queries SQLite for the read-only
    # admin "what did this user talk about" view. None on early boot
    # paths where SQLite isn't wired (tests); router-side guard returns
    # 503 in that case rather than crashing.
    app.state.session_factory = session_factory
    # Pairing guard: issuing a device token for an unprovisioned memory
    # user creates a chat session that can answer but will never recall or
    # persist long-term memory. Keep this state optional so router unit
    # tests can mount pairing in isolation.
    app.state.memory_routes = memory_routes
    app.state.memory_discovery_refresher = memory_discovery_refresher

    app.include_router(devices.router, prefix="/api/admin", tags=["devices"])
    app.include_router(personas.router, prefix="/api/admin", tags=["personas"])
    # ``templates`` MUST come after ``personas`` because both mount routes
    # under ``/personas/templates/*`` — personas has the read endpoints
    # (list/detail/raw/render) on /personas/templates while templates has
    # the write endpoints (POST/PUT/DELETE/fork). FastAPI matches in
    # registration order; the read-side patterns are fine following the
    # write-side because the path segments differ.
    app.include_router(templates.router, prefix="/api/admin", tags=["templates"])
    app.include_router(pairing_router.router, prefix="/api/admin", tags=["pairing"])
    app.include_router(chat_test.router, prefix="/api/admin", tags=["chat-test"])
    app.include_router(
        conversations.router, prefix="/api/admin", tags=["conversations"]
    )
    app.include_router(long_tasks.router, prefix="/api/admin", tags=["long-tasks"])
    app.include_router(reports.router, prefix="/api/admin", tags=["reports"])

    return app
