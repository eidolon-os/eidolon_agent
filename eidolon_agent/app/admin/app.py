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
    genome_authoring,
    long_tasks,
    reports,
)
from eidolon_agent.config.settings import Settings


def build_admin_app(
    *,
    settings: Settings,
    agent_registry,
    personas_service=None,
    custom_template_store=None,
    persona_template_registry=None,
    revocation_kv=None,
    data_store=None,
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
    app.state.data_store = data_store
    app.state.memory_routes = memory_routes
    app.state.memory_discovery_refresher = memory_discovery_refresher

    app.include_router(devices.router, prefix="/api/admin", tags=["devices"])
    app.include_router(chat_test.router, prefix="/api/admin", tags=["chat-test"])
    app.include_router(
        conversations.router, prefix="/api/admin", tags=["conversations"]
    )
    app.include_router(long_tasks.router, prefix="/api/admin", tags=["long-tasks"])
    app.include_router(reports.router, prefix="/api/admin", tags=["reports"])
    app.include_router(genome_authoring.router, prefix="/api/admin", tags=["genome-authoring"])

    return app
