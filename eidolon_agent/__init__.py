"""Eidolon Agent — desktop companion control plane.

This package is the *brain*: it is invoked by external LiveKit voice pipelines
via gRPC, talks to the external eidolon-memory service over MCP + NATS, and
forwards complex tasks to the workstation agent.

Public surface is intentionally narrow — most consumers should depend on the
Protocol types in :mod:`eidolon_agent.core.ports` or the data structures in
:mod:`eidolon_agent.core.types`, not on internal modules.
"""

__version__ = "0.1.0"
