"""Core HTTP application factory.

Health / readiness / liveness probes only. Admin APIs live in
:mod:`eidolon_agent.admin`.
"""

from __future__ import annotations

from fastapi import FastAPI

from eidolon_agent.app.transport.http.routers import health


def build_http_app(*, readiness) -> FastAPI:
    """Build the core HTTP app. ``readiness`` is a callable returning bool."""
    app = FastAPI(
        title="eidolon-agent",
        version="0.1.0",
        docs_url=None,
        openapi_url=None,
    )
    app.state.readiness = readiness
    app.include_router(health.router, tags=["health"])
    return app
