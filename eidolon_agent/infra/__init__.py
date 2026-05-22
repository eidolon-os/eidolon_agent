"""Infrastructure layer — adapters that implement ``core.ports``.

This layer wires the domain to external systems (LLMs, SQLite, NATS,
eidolon-memory MCP, log/metrics/trace backends). Domain modules must not
import from here directly; they receive instances through ``core.ports``.

Sub-packages:
    - ``llm/``        — LLM provider adapters (LiteLLM, Fake) + router
    - ``persistence/`` — SQLAlchemy 2.0 async + SQLite
    - ``events/``     — NATS event bus + JetStream KV store
    - ``memory/``     — eidolon-memory MCP client + NATS publisher
    - ``observability/`` — logging / metrics / tracing
"""
