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
    long_tasks,
    owner_runtime,
    reports,
)
from eidolon_agent.config.settings import Settings


def build_admin_app(
    *,
    settings: Settings,
    agent_registry,
    personas_service=None,
    revocation_kv=None,
    runtime_authority=None,
    runtime_store=None,
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
    app.state.runtime_authority = runtime_authority
    app.state.runtime_store = runtime_store
    app.state.memory_routes = memory_routes
    app.state.memory_discovery_refresher = memory_discovery_refresher

    app.include_router(
        owner_runtime.router,
        prefix="/api/admin",
        tags=["owner-runtime"],
    )
    app.include_router(chat_test.router, prefix="/api/admin", tags=["chat-test"])
    app.include_router(conversations.router, prefix="/api/admin", tags=["conversations"])
    app.include_router(long_tasks.router, prefix="/api/admin", tags=["long-tasks"])
    app.include_router(reports.router, prefix="/api/admin", tags=["reports"])
    return app
