"""Runtime: bootstrap, DI container, lifecycle, CLI entrypoint."""

from eidolon_agent.runtime.bootstrap import build_application
from eidolon_agent.runtime.container import Container

__all__ = ["Container", "build_application"]
