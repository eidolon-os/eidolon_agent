"""Application layer — process wiring + I/O surface.

This is the only layer allowed to import from every other layer. It owns:
    - ``runtime/``   — bootstrap, container, CLI, lifecycle
    - ``transport/`` — gRPC, HTTP (health), pairing
    - ``admin/``     — independent FastAPI admin app

``config/`` is at the package root (not under ``app/``) because settings
are pure Pydantic data models read by all layers at startup.
"""
