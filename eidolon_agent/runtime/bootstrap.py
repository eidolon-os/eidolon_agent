"""Bootstrap — the 15-step start-up sequence that wires the whole process.

This is the only place that knows the concrete dependency graph. Tests can
replace any step by passing pre-built collaborators in via ``overrides``.

Step list (matches the plan):

1.  init logging / tracing / metrics
2.  build DI container
3.  connect SQLite, NATS (+ KV buckets ensure)
4.  probe memory MCP endpoints
5.  PersonaTemplateRegistry.load_all + watcher
6.  PersonaOverlayStore bootstrap
7.  AgentRegistry.bootstrap (recover instances)
8.  NATS subscribers (memory.event / workstation.progress / persona.evolution.proposed)
9.  Register builtin hooks / tools / providers
10. ProactiveEngine start
11. SessionManager bootstrap (deferred)
12. Pairing coordinator + token verifier
13. gRPC server start
14. HTTP server start (FastAPI)
15. signal ready
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

from eidolon_agent.admin import build_admin_app
from eidolon_agent.agent.companion import CompanionAgent
from eidolon_agent.agent.registry import AgentRegistry, AgentTemplate
from eidolon_agent.agent.turn import TurnEngine
from eidolon_agent.brain import LLMRouter
from eidolon_agent.brain.llm.fake import FakeLLM
from eidolon_agent.config.settings import Settings, load_settings
from eidolon_agent.context.compiler import ContextCompiler
from eidolon_agent.context.providers import (
    HistoryProvider,
    MemoryRecallProvider,
    MindStateProvider,
    PersonaContextProvider,
    RealtimeSignalProvider,
)
from eidolon_agent.dispatch import NatsWorkstationClient, TaskClassifier
from eidolon_agent.events import InMemoryEventBus, InMemoryKVStore, NatsEventBus, NatsKVStore
from eidolon_agent.events.nats_bus import ensure_buckets
from eidolon_agent.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.history import HistoryFanout, HistoryManager
from eidolon_agent.hooks import HookExecutor
from eidolon_agent.memory import EidolonMemoryPort
from eidolon_agent.memory.mcp_client import McpClientPool
from eidolon_agent.memory.nats_pub import MemoryNatsPublisher
from eidolon_agent.mind import MindStateService
from eidolon_agent.observability import configure_logging
from eidolon_agent.observability.tracing import configure_tracing
from eidolon_agent.persistence import (
    create_engine,
    create_session_factory,
    ensure_schema,
)
from eidolon_agent.persona import (
    EvolutionPlanner,
    PersonaOverlayStore,
    PersonaResolver,
    PersonaTemplateRegistry,
)
from eidolon_agent.proactive import ProactiveEngine
from eidolon_agent.runtime.container import Container
from eidolon_agent.signals import SignalBus, SignalFuser
from eidolon_agent.tools import ToolDispatcher, ToolRegistry
from eidolon_agent.tools.builtin import EmitEventTool, GetTimeTool, SetMoodTool
from eidolon_agent.transport.grpc import GrpcServer
from eidolon_agent.transport.grpc.chat_servicer import EidolonAgentServicer
from eidolon_agent.transport.http import build_http_app
from eidolon_agent.transport.pairing import PairingCoordinator, PairingTokenVerifier

_log = logging.getLogger(__name__)


async def build_application(
    *,
    settings: Settings | None = None,
    use_inmem_nats: bool = False,
) -> Container:
    """Construct and connect everything. Idempotent within a single process.

    Pass ``use_inmem_nats=True`` in dev / tests to skip the NATS server and use
    the in-memory bus (handy for first-run before infra is up).
    """
    settings = settings or load_settings()
    container = Container(settings=settings)

    # 1. logging / tracing / metrics ------------------------------------------
    configure_logging(settings.observability)
    configure_tracing(settings.observability)

    # 2. container -------------------------------------------------------------
    # (already created above)

    # 3. SQLite + NATS ---------------------------------------------------------
    engine = create_engine(settings.sqlite)
    await ensure_schema(engine)
    session_factory = create_session_factory(engine)
    container.sqlite_engine = engine
    container.session_factory = session_factory

    if use_inmem_nats:
        bus: object = InMemoryEventBus()
        container.event_bus = bus
        container.kv_buckets = {name: InMemoryKVStore(name) for name in settings.nats.kv_buckets}
    else:
        nats_bus = NatsEventBus(settings.nats.url, creds_path=str(settings.nats.creds_path) if settings.nats.creds_path else None)
        await nats_bus.connect()
        await ensure_buckets(nats_bus, settings.nats.kv_buckets)
        container.event_bus = nats_bus
        container.kv_buckets = {name: NatsKVStore(nats_bus, name) for name in settings.nats.kv_buckets}
    cache_kv = container.kv_buckets.get("EIDOLON_CACHE")
    revocation_kv = container.kv_buckets.get("DEVICE_REVOCATIONS")

    # 4. Memory MCP probe ------------------------------------------------------
    mem_pool = McpClientPool(
        endpoints={e.user_id: e.mcp_url for e in settings.memory.endpoints},
        bearer_tokens={
            e.user_id: e.bearer_token
            for e in settings.memory.endpoints
            if e.bearer_token
        },
    )
    mem_pub = MemoryNatsPublisher(event_bus=container.event_bus)
    memory_port = EidolonMemoryPort(
        pool=mem_pool, publisher=mem_pub, cache_kv=cache_kv,
        cache_ttl_s=settings.memory.recall_cache_ttl_s,
    )
    container.memory_port = memory_port

    # 5 + 6. Persona templates + overlays --------------------------------------
    tpl_reg = PersonaTemplateRegistry(
        Path(settings.persona.templates_dir),
        event_bus=container.event_bus,
        watch=settings.persona.watch_enabled,
    )
    await tpl_reg.start()
    overlay_store = PersonaOverlayStore(Path(settings.persona.overlays_dir))
    resolver = PersonaResolver(tpl_reg, overlay_store, kv_store=cache_kv)
    evolution = EvolutionPlanner(tpl_reg, overlay_store, resolver, event_bus=container.event_bus)
    container.template_registry = tpl_reg
    container.overlay_store = overlay_store
    container.persona_resolver = resolver
    container.evolution_planner = evolution

    # 7-10. Cross-cutting services --------------------------------------------
    history = HistoryManager()
    fanout = HistoryFanout(event_bus=container.event_bus)
    mind = MindStateService()
    sig_bus = SignalBus()
    sig_fuser = SignalFuser(sig_bus)
    container.history_manager = history
    container.history_fanout = fanout
    container.mind_service = mind
    container.signal_bus = sig_bus
    container.signal_fuser = sig_fuser
    container.crisis_handler = CrisisHandler(event_bus=container.event_bus)
    container.input_guardrail = InputGuardrail()
    container.output_guardrail = OutputGuardrail()
    container.triage_classifier = TaskClassifier()
    container.proactive_engine = ProactiveEngine(event_bus=container.event_bus)

    # 9. Tools -----------------------------------------------------------------
    tool_registry = ToolRegistry()
    tool_registry.register(GetTimeTool())
    tool_registry.register(SetMoodTool(mind_service=mind))
    tool_registry.register(EmitEventTool(event_bus=container.event_bus))
    idemp_kv = container.kv_buckets.get("EIDOLON_TOOL_IDEMP")
    tool_dispatcher = ToolDispatcher(tool_registry, idempotency_store=idemp_kv)
    container.tool_registry = tool_registry
    container.tool_dispatcher = tool_dispatcher

    # 11. LLM router (provider wiring is intentionally minimal — fake by default;
    # production replaces via settings.llm.providers).
    llm_router = _build_llm_router(settings)
    container.llm_router = llm_router

    # 12. Dispatch (workstation) ----------------------------------------------
    if settings.workstation.transport == "nats":
        container.dispatch_port = NatsWorkstationClient(
            container.event_bus,
            request_timeout_s=settings.workstation.request_timeout_s,
        )

    # 13. Pairing --------------------------------------------------------------
    jwt_secret = settings.pairing.jwt_secret
    if not jwt_secret:
        jwt_secret = _generate_persisted_secret(Path(settings.runtime.run_dir) / "jwt-secret")
    pairing = PairingCoordinator(
        jwt_secret=jwt_secret,
        jwt_algorithm=settings.pairing.jwt_algorithm,
        code_ttl_s=settings.pairing.pairing_code_ttl_s,
        code_length=settings.pairing.pairing_code_length,
        token_ttl_days=settings.pairing.device_token_ttl_days,
    )
    verifier = PairingTokenVerifier(
        secret=jwt_secret,
        algorithm=settings.pairing.jwt_algorithm,
        revocation_kv=revocation_kv,
    )
    container.pairing_coordinator = pairing
    container.pairing_verifier = verifier

    # 14. AgentRegistry with instance factory closure ------------------------
    async def _build_companion(inst):  # type: ignore[no-untyped-def]
        engine = _build_turn_engine(
            container=container,
            instance_id=inst.instance_id,
            template_id=inst.template_id,
        )
        return CompanionAgent(instance_id=inst.instance_id, turn_engine=engine)

    agent_registry = AgentRegistry(instance_factory=_build_companion)
    # Register one default template per loaded persona.
    for tpl in tpl_reg.list_all():
        agent_registry.register_template(
            AgentTemplate(template_id=tpl.template_id, name=tpl.name, description=tpl.description)
        )
    container.agent_registry = agent_registry

    # 15. Transport servers ---------------------------------------------------
    servicer = EidolonAgentServicer(
        agent_registry=agent_registry,
        pairing=pairing,
        signals_bus=sig_bus,
        proactive_bus=container.event_bus,
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
        pairing=pairing,
        template_registry=tpl_reg,
        overlay_store=overlay_store,
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
    from eidolon_agent.brain.llm import LiteLLMProvider

    providers: dict[str, object] = {"fake": FakeLLM()}
    for m in settings.llm.models:
        try:
            providers[m.name] = LiteLLMProvider(
                model=m.name, api_key=m.api_key, api_base=m.api_base, timeout_s=m.timeout_s,
            )
        except Exception:
            _log.warning("model %s not loaded", m.name)

    default = settings.llm.default_model
    if default not in providers:
        _log.warning("default_model %s not configured, falling back to fake", default)
        default = "fake"
    return LLMRouter(providers=providers, default=default)


def _build_turn_engine(
    *,
    container: Container,
    instance_id: str,
    template_id: str,
) -> TurnEngine:
    """Construct a per-instance TurnEngine with provider closures."""

    def locator(_tenant_id: str, _user_id: str, _conv_id: str):
        return (instance_id, template_id)

    persona_p = PersonaContextProvider(resolver=container.persona_resolver, instance_locator=locator)
    history_p = HistoryProvider(history_manager=container.history_manager, window=20)
    memory_p = MemoryRecallProvider(memory_port=container.memory_port)
    mind_p = MindStateProvider(mind_service=container.mind_service)
    realtime_p = RealtimeSignalProvider(signal_fuser=None)
    compiler = ContextCompiler(
        [persona_p, history_p, memory_p, mind_p, realtime_p],
        max_token_budget=container.settings.turn.max_token_budget,
    )
    return TurnEngine(
        compiler=compiler,
        llm=container.llm_router,
        tool_dispatcher=container.tool_dispatcher,
        hook_executor=HookExecutor(),
        history=container.history_manager,
        fanout=container.history_fanout,
        triage=container.triage_classifier,
        input_guardrail=container.input_guardrail,
        output_guardrail=container.output_guardrail,
        crisis=container.crisis_handler,
        dispatch_port=container.dispatch_port,
        event_bus=container.event_bus,
        max_tool_iters=container.settings.turn.max_tool_iters,
        taboos_provider=lambda: tuple(),
    )


def _generate_persisted_secret(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    path.write_text(secret, encoding="utf-8")
    path.chmod(0o600)
    return secret
