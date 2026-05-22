"""Runtime: bootstrap, DI container, lifecycle, CLI entrypoint."""

from eidolon_agent.app.runtime.bootstrap import build_application
from eidolon_agent.app.runtime.container import Container

__all__ = ["Container", "build_application"]
