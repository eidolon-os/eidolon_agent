"""Bootstrap — startup sequence that wires the whole process.

This is the only place that knows the concrete dependency graph. Steps:

1.  init logging
2.  build DI container
3.  connect SQLite, NATS (+ KV buckets ensure)
4.  probe memory MCP endpoints
5.  Personas registry + per-user instance store
6.  Cross-cutting services (history, signals, guardrails, triage)
7.  Tools + LLM router + dispatch
8.  Runtime token verifier
9.  AgentRegistry with instance factory closure
10. gRPC + HTTP + Admin transport servers
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

import httpx
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
from eidolon_agent.domain.agent.registry import AgentRegistry, AgentTemplate
from eidolon_agent.domain.agent.triage import TaskClassifier
from eidolon_agent.domain.agent.turn import ToolLatencyPolicy, TurnEngine
from eidolon_agent.domain.body_control import (
    BodyControlService,
    CachedBodyDeviceStore,
    EidolonDataBodyDeviceStore,
    HubBodyCommandClient,
)
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.domain.harness import HarnessBudget, RealtimeAgentHarness
from eidolon_agent.domain.history import HistoryFanout, HistoryManager
from eidolon_agent.domain.long_tasks import LongTaskResultSummarizer
from eidolon_agent.domain.personas import (
    PersonasService,
    PersonaTemplateRegistry,
    YamlPersonaInstanceStore,
)
from eidolon_agent.domain.personas.ports import PersonaEventPort
from eidolon_agent.domain.signals import SignalBus
from eidolon_agent.domain.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.domain.tools.builtin import (
    ControlBodyDeviceTool,
    EmitEventTool,
    GetBodyCommandStatusTool,
    GetTimeTool,
    GetWeatherTool,
    ListBodyDevicesTool,
    MemoryAssertFactTool,
    MemoryForgetTool,
    MemorySearchTool,
    SubmitLongTaskTool,
)
from eidolon_agent.infra.events import NatsEventBus, NatsKVStore
from eidolon_agent.infra.events.nats_bus import ensure_buckets
from eidolon_agent.infra.llm import LLMRouter
from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.long_tasks import MementosHttpClient, MementosLongTaskWorker
from eidolon_agent.infra.long_tasks.mementos import MementosWorkerConfig
from eidolon_agent.infra.memory import EidolonMemoryPort
from eidolon_agent.infra.memory.discovery import build_initial_memory_routes
from eidolon_agent.infra.memory.mcp_client import McpClientPool
from eidolon_agent.infra.memory.nats_pub import MemoryNatsPublisher
from eidolon_agent.infra.observability import configure_logging
from eidolon_agent.infra.persistence.eidolon_data_persona import (
    EidolonDataCustomTemplateStore,
    EidolonDataEvolutionHistoryStore,
    EidolonDataPersonaEvolutionProposalStore,
    EidolonDataPersonaInstanceStore,
    EidolonDataPersonaObservationStore,
)
from eidolon_agent.infra.persistence.eidolon_data_runtime import (
    EidolonDataLongTaskStore,
    EidolonDataMemoryFanoutStatusSink,
    build_eidolon_data_history_hydrator,
    build_eidolon_data_turn_persister,
)

_log = logging.getLogger(__name__)


async def build_application(
    *,
    settings: Settings | None = None,
) -> Container:
    """Construct and connect everything. Idempotent within a single process."""
    settings = settings or load_settings()
    container = Container(settings=settings)

    # 1. logging ----------------------------------------------------------------
    configure_logging(settings.observability)

    # 2. container -------------------------------------------------------------
    # (already created above)

    # 3. Eidolon Data + memory discovery + NATS -------------------------------
    data_store = DataStore.open(load_data_settings())
    await data_store.init_schema()
    container.data_store = data_store

    memory_routes, effective_nats_url, memory_refresher = await build_initial_memory_routes(
        memory=settings.memory,
        nats=settings.nats,
    )
    container.extras["memory_routes"] = memory_routes

    nats_bus = NatsEventBus(
        effective_nats_url,
        creds_path=str(settings.nats.creds_path) if settings.nats.creds_path else None,
    )
    await nats_bus.connect()
    await ensure_buckets(nats_bus, settings.nats.kv_buckets)
    container.event_bus = nats_bus
    container.kv_buckets = {name: NatsKVStore(nats_bus, name) for name in settings.nats.kv_buckets}
    revocation_kv = container.kv_buckets.get("DEVICE_REVOCATIONS")

    # 4. Memory MCP probe ------------------------------------------------------
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

    # 5 + 6. Personas templates + per-user instance copies ---------------------
    # Templates have two backing stores: builtin yaml files (read-only,
    # ship with the agent) and operator-mutable presets in eidolon_data.
    # The registry merges them at lookup time.
    custom_template_store = EidolonDataCustomTemplateStore(data_store)
    tpl_reg = PersonaTemplateRegistry(
        Path(settings.persona.templates_dir),
        custom_source=custom_template_store,
    )
    await tpl_reg.load_all()
    container.custom_template_store = custom_template_store
    container.persona_template_registry = tpl_reg
    # Production wiring: eidolon_data-backed persona genomes. The legacy
    # YamlPersonaInstanceStore remains available for diagnostic / forensic
    # scenarios.
    if settings.persona.storage == "yaml":
        instance_store: object = YamlPersonaInstanceStore(Path(settings.persona.instances_dir))
    else:
        instance_store = EidolonDataPersonaInstanceStore(data_store)
    # One adapter satisfies both PersonaAuditPort (write) and
    # PersonaEvolutionRepository (read) so worker writes audit rows AND admin
    # can paginate them. NullPersonaAuditPort is no longer used in production.
    evolution_history = EidolonDataEvolutionHistoryStore(data_store)
    persona_observations = EidolonDataPersonaObservationStore(data_store)
    persona_proposals = EidolonDataPersonaEvolutionProposalStore(data_store)
    personas_service = PersonasService(
        registry=tpl_reg,
        instances=instance_store,
        llm_port=None,
        event_port=_PersonasEventAdapter(container.event_bus),
        audit_port=evolution_history,
        evolution_repo=evolution_history,
        observation_repo=persona_observations,
        proposal_repo=persona_proposals,
    )
    await personas_service.start()
    container.persona_instance_store = instance_store
    container.persona_observation_store = persona_observations
    container.persona_proposal_store = persona_proposals
    container.personas_service = personas_service

    # 7. Cross-cutting services -----------------------------------------------
    history = HistoryManager(hydrate_messages=build_eidolon_data_history_hydrator(data_store))
    fanout = HistoryFanout(
        event_bus=container.event_bus,
        memory_routes=memory_routes,
        status_sink=EidolonDataMemoryFanoutStatusSink(data_store),
    )
    background_tasks = BackgroundTaskRunner(component="agent")
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
    if settings.long_task.transport == "mementos_http":
        long_task_worker = MementosLongTaskWorker(
            store=EidolonDataLongTaskStore(data_store),
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
            result_summarizer=LongTaskResultSummarizer(llm_router),
            event_bus=container.event_bus,
        )
        long_task_worker.start()
        container.extras["long_task_worker"] = long_task_worker
    tool_registry = ToolRegistry()
    tool_registry.register(GetTimeTool())
    tool_registry.register(GetWeatherTool())
    explicit_memory_timeout_s = settings.turn.explicit_memory_recall_timeout_ms / 1000
    tool_registry.register(MemorySearchTool(memory_port, timeout_s=explicit_memory_timeout_s))
    tool_registry.register(MemoryAssertFactTool(memory_port))
    tool_registry.register(MemoryForgetTool(memory_port))
    body_control = None
    if settings.body_control.enabled:
        body_http_client = httpx.AsyncClient(timeout=settings.body_control.timeout_s)
        body_command_client = HubBodyCommandClient(
            body_http_client,
            base_url=settings.body_control.hub_base_url,
            timeout_s=settings.body_control.timeout_s,
        )
        body_device_store = CachedBodyDeviceStore(
            EidolonDataBodyDeviceStore(
                data_store,
                runtime_client=body_command_client,
            ),
            ttl_s=settings.body_control.cache_ttl_s,
        )
        body_control = BodyControlService(
            device_store=body_device_store,
            command_port=body_command_client,
        )
        container.extras["body_device_store"] = body_device_store
        container.extras["body_control_http_client"] = body_http_client
        container.extras["body_control"] = body_control
    tool_registry.register(ListBodyDevicesTool(body_control))
    tool_registry.register(ControlBodyDeviceTool(body_control))
    tool_registry.register(GetBodyCommandStatusTool(body_control))
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

    templates = list(tpl_reg.list_all())
    default_genome_id = templates[0].metadata.template_id if templates else ""
    agent_registry = AgentRegistry(
        instance_factory=_build_companion,
        default_genome_id=default_genome_id,
    )
    for tpl in templates:
        agent_registry.register_template(
            AgentTemplate(
                genome_id=tpl.metadata.template_id,
                name=tpl.metadata.name,
                description=tpl.metadata.description,
            )
        )
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
        custom_template_store=custom_template_store,
        persona_template_registry=tpl_reg,
        # Phase 33.B1: admin /users/{id}/revoke-sessions writes here;
        # same instance the verifier reads. Same bucket, two consumers.
        revocation_kv=revocation_kv,
        memory_routes=memory_routes,
        memory_discovery_refresher=memory_refresher,
        data_store=data_store,
    )
    container.http_app = http_app
    container.admin_app = admin_app

    _log.info(
        "bootstrap done: %d templates, %d kv buckets, %d memory endpoints",
        len(tpl_reg.list_all()),
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
            memory_timeout_ms=container.settings.turn.memory_recall_soft_timeout_ms,
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
        memory_timeout_s=container.settings.turn.memory_recall_soft_timeout_ms / 1000,
        explicit_memory_timeout_s=(
            container.settings.turn.explicit_memory_recall_timeout_ms / 1000
        ),
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
        persona_template_id=genome_id,
        memory_port=container.memory_port,
        max_tool_iters=container.settings.turn.max_tool_iters,
        memory_write_mode=container.settings.turn.memory_write_mode,
        tool_schema_strict=container.settings.turn.tool_schema_strict,
        require_idempotency_for_side_effect_tools=container.settings.turn.require_idempotency_for_side_effect_tools,
        taboos_provider=lambda: tuple(),
        turn_persister=build_eidolon_data_turn_persister(
            container.data_store,
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


class _PersonasEventAdapter(PersonaEventPort):
    def __init__(self, event_bus) -> None:
        self._bus = event_bus

    async def publish_persona_updated(self, instance_id: str, payload: dict) -> None:
        if self._bus is None:
            return
        from eidolon_agent.core.types.event import Event
        from eidolon_agent.core.types.topics import Topics

        await self._bus.publish(
            Event(
                subject=Topics.persona_overlay_updated(instance_id),
                payload=payload,
                source="personas.service",
            )
        )

    async def publish_evolution_applied(self, instance_id: str, payload: dict) -> None:
        if self._bus is None:
            return
        from eidolon_agent.core.types.event import Event
        from eidolon_agent.core.types.topics import Topics

        await self._bus.publish(
            Event(
                subject=Topics.evolution_applied(instance_id),
                payload=payload,
                source="personas.service",
            ),
            persistent=True,
        )
