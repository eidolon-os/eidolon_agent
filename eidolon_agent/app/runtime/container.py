"""Tiny DI container — a typed bag of singletons.

We intentionally avoid a heavyweight DI framework. The container holds
everything constructed during bootstrap so feature modules can grab their
collaborators without import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Container:
    """Heterogeneous service registry. Fields are populated by the bootstrap."""

    settings: object | None = None
    event_bus: object | None = None
    kv_buckets: dict[str, object] = field(default_factory=dict)

    sqlite_engine: object | None = None
    session_factory: object | None = None
    local_system_data: object | None = None
    runtime_authority: object | None = None
    runtime_session_authorizer: object | None = None
    runtime_store: object | None = None

    persona_genome_store: object | None = None
    personas_service: object | None = None

    history_manager: object | None = None
    history_fanout: object | None = None
    background_tasks: object | None = None
    signal_bus: object | None = None
    crisis_handler: object | None = None
    input_guardrail: object | None = None
    output_guardrail: object | None = None
    triage_classifier: object | None = None

    tool_registry: object | None = None
    tool_dispatcher: object | None = None

    llm_router: object | None = None
    memory_port: object | None = None

    agent_registry: object | None = None
    runtime_token_verifier: object | None = None

    grpc_server: object | None = None
    http_app: object | None = None
    admin_app: object | None = None

    extras: dict[str, object] = field(default_factory=dict)
