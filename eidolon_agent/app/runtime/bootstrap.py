"""Bootstrap — startup sequence that wires the whole process.

This is the only place that knows the concrete dependency graph. Steps:

1.  init logging
2.  build DI container
3.  connect SQLite, NATS (+ KV buckets ensure)
4.  probe memory MCP endpoints
5.  Canonical persona genome store
6.  Cross-cutting services (history, signals, guardrails, triage)
7.  Tools + LLM router + dispatch
8.  Runtime token verifier
9.  AgentRegistry with instance factory closure
10. gRPC + HTTP + Admin transport servers
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path

from eidolon_data import DataStore
from eidolon_data import load_settings as load_data_settings
from eidolon_sdk.biz.runtime import RuntimeTokenVerifier
from eidolon_sdk.core.runtime import BackgroundTaskRunner

from eidolon_agent.app.admin import build_admin_app
from eidolon_agent.app.runtime.container import Container
from eidolon_agent.app.transport.grpc import GrpcServer
from eidolon_agent.app.transport.grpc.chat_servicer import EidolonAgentServicer
from eidolon_agent.app.transport.http import build_http_app
from eidolon_agent.config.settings import Settings, load_settings
from eidolon_agent.core.types.tool import Permission
from eidolon_agent.domain.agent.companion import CompanionAgent
from eidolon_agent.domain.agent.companion_config import CompanionConfigResolver
from eidolon_agent.domain.agent.registry import AgentRegistry
from eidolon_agent.domain.agent.triage import TaskClassifier
from eidolon_agent.domain.agent.turn import ToolLatencyPolicy, TurnEngine
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.domain.harness import HarnessBudget, RealtimeAgentHarness
from eidolon_agent.domain.history import HistoryFanout, HistoryManager
from eidolon_agent.domain.long_tasks import LongTaskResultSummarizer
from eidolon_agent.domain.personas import PersonasService, PersonaVoice
from eidolon_agent.domain.signals import SignalBus
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.body_capability_provider import RuntimeCapabilityToolProvider
from eidolon_agent.domain.tools.builtin import (
    EmitEventTool,
    GetTimeTool,
    GetWeatherTool,
    MemoryAssertFactTool,
    MemoryConfirmPendingTool,
    MemoryForgetTool,
    MemorySearchTool,
    MemoryStageCandidateTool,
    PendingMemoryCandidateStore,
    SubmitLongTaskTool,
)
from eidolon_agent.infra.events import NatsEventBus, NatsKVStore
from eidolon_agent.infra.events.adapters.inmem import InMemoryEventBus, InMemoryKVStore
from eidolon_agent.infra.events.nats_bus import ensure_buckets
from eidolon_agent.infra.llm import LLMRouter
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.long_tasks import MementosHttpClient, MementosLongTaskWorker
from eidolon_agent.infra.long_tasks.mementos import MementosWorkerConfig
from eidolon_agent.infra.memory import EidolonMemoryPort
from eidolon_agent.infra.memory.discovery import build_initial_memory_routes
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher
from eidolon_agent.infra.memory.null_port import NullMemoryPort
from eidolon_agent.infra.observability import configure_logging
from eidolon_agent.infra.persistence.agent_runtime import (
    AgentLongTaskStore,
    build_agent_history_hydrator,
    build_agent_turn_persister,
)
from eidolon_agent.infra.persistence.audit_dispatch import run_agent_audit_dispatcher
from eidolon_agent.infra.persistence.eidolon_data_persona import (
    EidolonDataPersonaGenomeStore,
)
from eidolon_agent.infra.persistence.runtime_store import AgentRuntimeStore

_log = logging.getLogger(__name__)


async def build_application(
    *,
    settings: Settings | None = None,
) -> Container:
    """Construct and connect everything. Idempotent within a single process."""
    settings = settings or load_settings()
    if settings.body_control.enabled:
        raise RuntimeError(
            "body_control is blocked: Channel has no stable Provider "
            "directory/command contract; Hub is not a command runtime"
        )
    container = Container(settings=settings)

    # 1. logging ----------------------------------------------------------------
    configure_logging(settings.observability)

    # 2. container -------------------------------------------------------------
    # (already created above)

    # 3. Authority stores + memory discovery + NATS ---------------------------
    standalone = settings.runtime.standalone
    runtime_store = AgentRuntimeStore.open(
        settings.persistence.sqlite_path,
        busy_timeout_ms=settings.persistence.busy_timeout_ms,
        wal_autocheckpoint_pages=settings.persistence.wal_autocheckpoint_pages,
    )
    await runtime_store.init_schema()
    container.runtime_store = runtime_store

    # System Data is opened separately for low-frequency Companion/Persona
    # authority. It must never receive session/turn/message/job writes.
    data_store = DataStore.open(load_data_settings())
    await data_store.init_schema()
    container.data_store = data_store

    memory_refresher = None
    if standalone:
        # Self-contained profile: no NATS, no memory service, no coworker.
        # In-process bus + KV and a null memory port so the whole brain runs
        # offline. Fanout/publisher tolerate memory_routes=None (they fall
        # back to the canonical subject), so wiring stays on the normal path.
        _log.info("bootstrap: standalone profile — in-process bus + null memory")
        memory_routes = None
        container.event_bus = InMemoryEventBus()
        container.kv_buckets = {
            name: InMemoryKVStore(bucket=name) for name in settings.nats.kv_buckets
        }
        memory_port: object = NullMemoryPort()
        container.memory_port = memory_port
    else:
        memory_routes, effective_nats_url, memory_refresher = (
            await build_initial_memory_routes(
                memory=settings.memory,
                nats=settings.nats,
            )
        )
        container.extras["memory_routes"] = memory_routes

        nats_bus = NatsEventBus(
            effective_nats_url,
            creds_path=str(settings.nats.creds_path) if settings.nats.creds_path else None,
        )
        try:
            await nats_bus.connect()
            await ensure_buckets(nats_bus, settings.nats.kv_buckets)
        except Exception as exc:
            raise RuntimeError(
                f"eidolon-agent could not connect to NATS at {effective_nats_url!r} "
                f"({type(exc).__name__}: {exc}). Start NATS (JetStream) or run with "
                f"runtime.standalone=true for the in-process profile."
            ) from exc
        container.event_bus = nats_bus
        container.kv_buckets = {
            name: NatsKVStore(nats_bus, name) for name in settings.nats.kv_buckets
        }

        # 4. Memory MCP probe --------------------------------------------------
        mem_pool = McpClientPool(routes=memory_routes)
        mem_pub = MemoryNatsPublisher(event_bus=container.event_bus, routes=memory_routes)
        memory_port = EidolonMemoryPort(
            pool=mem_pool,
            publisher=mem_pub,
        )
        container.memory_port = memory_port
        if memory_refresher is not None:
            memory_refresher.start()
            container.extras["memory_discovery_refresher"] = memory_refresher
    revocation_kv = container.kv_buckets.get("DEVICE_REVOCATIONS")

    # 5 + 6. Persona snapshots -------------------------------------------------
    # The database is the sole persona source of truth. Runtime sessions pin an
    # immutable genome id/hash and never select a template or fallback persona.
    persona_store = EidolonDataPersonaGenomeStore(data_store)
    personas_service = PersonasService(store=persona_store)
    await personas_service.start()
    container.persona_genome_store = persona_store
    container.personas_service = personas_service

    # 7. Cross-cutting services -----------------------------------------------
    history = HistoryManager(hydrate_messages=build_agent_history_hydrator(runtime_store))
    fanout = HistoryFanout(
        event_bus=container.event_bus,
        memory_routes=memory_routes,
        # Publish/absorb status is operational telemetry. Keeping it in the
        # shared system-data SQLite made this per-turn async path the largest
        # Event-table producer without providing a governance guarantee.
        status_sink=None,
    )
    background_tasks = BackgroundTaskRunner(component="agent")
    if not standalone:
        container.extras["audit_dispatch_task"] = asyncio.create_task(
            run_agent_audit_dispatcher(
                runtime_store,
                nats_url=effective_nats_url,
            ),
            name="eidolon-agent-audit-dispatcher",
        )
    sig_bus = SignalBus()
    container.history_manager = history
    container.history_fanout = fanout
    container.background_tasks = background_tasks
    container.signal_bus = sig_bus
    container.crisis_handler = CrisisHandler(event_bus=container.event_bus)
    container.input_guardrail = InputGuardrail()
    container.output_guardrail = OutputGuardrail()
    container.triage_classifier = TaskClassifier()

    # 9. LLM router ------------------------------------------------------------
    llm_router = _build_llm_router(settings)
    container.llm_router = llm_router
    if settings.runtime.warmup_enabled and settings.llm.startup_warm_enabled:
        try:
            await llm_router.warmup_default(timeout_s=settings.llm.startup_warm_timeout_s)
        except Exception:
            _log.warning("llm warmup failed; continuing startup", exc_info=True)

    # 10. Tools ----------------------------------------------------------------
    long_task_worker = None
    if settings.long_task.transport == "mementos_http" and not standalone:
        long_task_worker = MementosLongTaskWorker(
            store=AgentLongTaskStore(runtime_store),
            client=MementosHttpClient(
                base_url=settings.long_task.mementos_base_url,
                timeout_s=settings.long_task.worker_http_timeout_s,
            ),
            config=MementosWorkerConfig(
                base_url=settings.long_task.mementos_base_url,
                queue_size=settings.long_task.queue_size,
                poll_interval_s=settings.long_task.worker_poll_interval_s,
                task_timeout_s=settings.long_task.worker_task_timeout_s,
                http_timeout_s=settings.long_task.worker_http_timeout_s,
                lease_s=settings.long_task.worker_lease_s,
            ),
            result_summarizer=LongTaskResultSummarizer(
                llm_router,
                persona_voice=PersonaVoice(personas_service),
            ),
            persona_voice=PersonaVoice(personas_service),
            event_bus=container.event_bus,
        )
        long_task_worker.start()
        container.extras["long_task_worker"] = long_task_worker
    tool_registry = ToolRegistry()
    tool_registry.register(GetTimeTool())
    tool_registry.register(GetWeatherTool())
    explicit_memory_timeout_s = settings.memory.explicit_recall_timeout_s
    tool_registry.register(MemorySearchTool(memory_port, timeout_s=explicit_memory_timeout_s))
    tool_registry.register(MemoryAssertFactTool(memory_port))
    pending_memory_candidates = PendingMemoryCandidateStore()
    container.extras["pending_memory_candidates"] = pending_memory_candidates
    tool_registry.register(MemoryStageCandidateTool(pending_memory_candidates))
    tool_registry.register(
        MemoryConfirmPendingTool(memory_port, pending_memory_candidates)
    )
    tool_registry.register(MemoryForgetTool(memory_port))
    tool_registry.register(EmitEventTool(event_bus=container.event_bus))
    delegate_tool = SubmitLongTaskTool(
        long_task_submitter=long_task_worker,
    )
    tool_registry.register(delegate_tool)
    idemp_kv = container.kv_buckets.get("EIDOLON_TOOL_IDEMP")
    tool_dispatcher = ToolDispatcher(
        tool_registry,
        idempotency_store=idemp_kv,
        allowed_permissions={p for p in Permission},
        schema_strict=settings.turn.tool_schema_strict,
        batch_timeout_s=settings.turn.tool_batch_timeout_s,
        require_idempotency_for_side_effect=settings.turn.require_idempotency_for_side_effect_tools,
    )
    container.tool_registry = tool_registry
    container.tool_dispatcher = tool_dispatcher
    # Per-companion operational config (model routing / tool allow-deny / policy),
    # read from companions.runtime_config_json, resolved per-turn off a TTL cache.
    container.extras["companion_config_resolver"] = CompanionConfigResolver(data_store)
    # Empty until a stable Channel Provider adapter implements the domain ports.
    container.extras["body_capability_tool_provider"] = RuntimeCapabilityToolProvider(
        None
    )

    # 8. Runtime token verification ------------------------------------------
    jwt_secret = settings.runtime_token.jwt_secret
    if not jwt_secret:
        jwt_secret = _generate_persisted_secret(Path(settings.runtime.run_dir) / "jwt-secret")
    verifier = RuntimeTokenVerifier(
        secret=jwt_secret,
        algorithm=settings.runtime_token.jwt_algorithm,
        revocation_kv=revocation_kv,
    )
    container.runtime_token_verifier = verifier

    # 14. AgentRegistry with instance factory closure ------------------------
    async def _build_companion(inst):  # type: ignore[no-untyped-def]
        engine = _build_turn_engine(
            container=container,
            companion_id=inst.companion_id,
            genome_id=inst.genome_id,
        )
        return CompanionAgent(companion_id=inst.companion_id, turn_engine=engine)

    agent_registry = AgentRegistry(instance_factory=_build_companion)
    container.agent_registry = agent_registry

    # 15. Transport servers ---------------------------------------------------
    servicer = EidolonAgentServicer(
        agent_registry=agent_registry,
        signals_bus=sig_bus,
        proactive_bus=container.event_bus,
        personas_service=personas_service,
    )
    grpc_server = GrpcServer(
        servicer=servicer,
        token_verifier=verifier,
        tcp_host=settings.grpc.tcp_host,
        tcp_port=settings.grpc.tcp_port,
        uds_path=settings.grpc.uds_path,
        keepalive_time_s=settings.grpc.keepalive_time_s,
        keepalive_timeout_s=settings.grpc.keepalive_timeout_s,
        max_connection_idle_s=settings.grpc.max_connection_idle_s,
    )
    container.grpc_server = grpc_server

    http_app = build_http_app(readiness=lambda: True)
    admin_app = build_admin_app(
        settings=settings,
        agent_registry=agent_registry,
        personas_service=personas_service,
        # Phase 33.B1: admin /users/{id}/revoke-sessions writes here;
        # same instance the verifier reads. Same bucket, two consumers.
        revocation_kv=revocation_kv,
        memory_routes=memory_routes,
        memory_discovery_refresher=memory_refresher,
        data_store=data_store,
        runtime_store=runtime_store,
    )
    container.http_app = http_app
    container.admin_app = admin_app

    _log.info(
        "bootstrap done: canonical persona store, %d kv buckets, %d memory endpoints",
        len(container.kv_buckets),
        len(settings.memory.endpoints),
    )
    return container


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_llm_router(settings: Settings) -> LLMRouter:
    """Build a router with LiteLLM providers from config + FakeLLM for tests."""
    from eidolon_agent.infra.llm.providers import LiteLLMProvider

    providers: dict[str, object] = {"fake": FakeLLM()}
    # Standalone runs offline — no provider credentials — so it must speak
    # through the deterministic FakeLLM, never a real endpoint.
    if settings.runtime.standalone:
        return LLMRouter(providers=providers, default="fake", fallback_models=())
    for m in settings.llm.models:
        try:
            providers[m.name] = LiteLLMProvider(
                model=m.name,
                api_key=m.resolved_api_key(),
                api_base=m.api_base,
                timeout_s=m.timeout_s,
                max_retries=settings.llm.max_retries,
                shared_http_client=settings.llm.shared_http_client,
            )
        except Exception:
            _log.warning("model %s not loaded", m.name)

    default = settings.llm.default_model
    if default not in providers:
        _log.warning("default_model %s not configured, falling back to fake", default)
        default = "fake"
    return LLMRouter(
        providers=providers,
        default=default,
        fallback_models=settings.llm.fallback_models,
    )


def _build_turn_engine(
    *,
    container: Container,
    companion_id: str,
    genome_id: str,
) -> TurnEngine:
    """Construct a per-companion TurnEngine with collaborator closures."""

    def locator(_owner_id: str, _companion_id: str, _conv_id: str):
        return (companion_id, genome_id)

    harness = RealtimeAgentHarness(
        budget=HarnessBudget(
            memory_timeout_ms=int(container.settings.memory.recall_timeout_s * 1000),
            history_window=container.settings.turn.history_context_window,
            max_tool_iters=container.settings.turn.max_tool_iters,
            first_delta_budget_ms=container.settings.turn.first_delta_slo_p95_ms,
            message_budget_tokens=container.settings.turn.max_token_budget,
            tool_schema_budget_tokens=container.settings.turn.tool_schema_budget_tokens,
            output_reserve_tokens=container.settings.turn.output_reserve_tokens,
        )
    )
    compiler = ContextCompiler(
        personas_service=container.personas_service,
        instance_locator=locator,
        history_manager=container.history_manager,
        memory_port=container.memory_port,
        history_window=harness.budget.history_window,
        degraded_history_window=container.settings.turn.degraded_history_context_window,
        memory_timeout_s=container.settings.memory.recall_timeout_s,
        explicit_memory_timeout_s=container.settings.memory.explicit_recall_timeout_s,
        context_budget_tokens=container.settings.turn.max_token_budget,
        context_budget_mode=container.settings.turn.context_budget_mode,
        harness=harness,
    )
    return TurnEngine(
        compiler=compiler,
        llm=container.llm_router,
        tool_dispatcher=container.tool_dispatcher,
        history=container.history_manager,
        fanout=container.history_fanout,
        triage=container.triage_classifier,
        input_guardrail=container.input_guardrail,
        output_guardrail=container.output_guardrail,
        crisis=container.crisis_handler,
        event_bus=container.event_bus,
        personas_service=container.personas_service,
        genome_id=genome_id,
        memory_port=container.memory_port,
        max_tool_iters=container.settings.turn.max_tool_iters,
        memory_write_mode=container.settings.turn.memory_write_mode,
        tool_schema_strict=container.settings.turn.tool_schema_strict,
        require_idempotency_for_side_effect_tools=container.settings.turn.require_idempotency_for_side_effect_tools,
        taboos_provider=lambda: tuple(),
        companion_config_resolver=container.extras.get("companion_config_resolver"),
        body_capability_provider=container.extras.get("body_capability_tool_provider"),
        turn_persister=build_agent_turn_persister(
            container.runtime_store,
            model_id_provider=lambda: getattr(container.llm_router, "model_id", None),
        ),
        harness=harness,
        background_tasks=container.background_tasks,
        tool_latency_policy=ToolLatencyPolicy(
            slow_hint_delay_s=container.settings.turn.slow_tool_hint_delay_ms / 1000,
        ),
    )


def _generate_persisted_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    path.write_text(secret, encoding="utf-8")
    path.chmod(0o600)
    return secret
