"""Admin HTTP control plane — independent FastAPI app for management APIs."""

from eidolon_agent.app.admin.app import build_admin_app

__all__ = ["build_admin_app"]
