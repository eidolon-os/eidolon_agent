"""Admin FastAPI application factory.

Mounts all admin routers under ``/api/admin``, serves the admin_web SPA build
(if present) at ``/``, and exposes OpenAPI docs at ``/api/docs``.

This app is independent of the core transport layer and can be mounted on its
own uvicorn instance or composed into a larger ASGI app.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from eidolon_agent.admin.routers import agents, chat_test, devices, personas
from eidolon_agent.admin.routers import pairing as pairing_router
from eidolon_agent.config.settings import Settings


def build_admin_app(
    *,
    settings: Settings,
    agent_registry,
    pairing,
    template_registry,
    overlay_store,
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
    app.state.template_registry = template_registry
    app.state.overlay_store = overlay_store

    app.include_router(agents.router, prefix="/api/admin", tags=["agents"])
    app.include_router(devices.router, prefix="/api/admin", tags=["devices"])
    app.include_router(personas.router, prefix="/api/admin", tags=["personas"])
    app.include_router(pairing_router.router, prefix="/api/admin", tags=["pairing"])
    app.include_router(chat_test.router, prefix="/api/admin", tags=["chat-test"])

    if settings.http.serve_admin_web_dist:
        dist = Path(settings.http.admin_web_dist_path)
        if dist.is_dir():
            app.mount("/", StaticFiles(directory=str(dist), html=True), name="admin_web")

    return app
