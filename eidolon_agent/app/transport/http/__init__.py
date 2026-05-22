"""Core HTTP transport — health probes only."""

from eidolon_agent.app.transport.http.app import build_http_app

__all__ = ["build_http_app"]
