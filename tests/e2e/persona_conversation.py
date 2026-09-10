"""Run the production TurnEngine with an HTTP-resolved persona and isolated history."""

from uuid import uuid4

from eidolon_sdk.biz.dialogue_control import CommittedTurnDecision, TurnCommitBoundary

from eidolon_agent.config import load_settings
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig
from eidolon_agent.core.types.memory import MemoryRecallResult
from eidolon_agent.core.types.turn import TurnInput, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.agent.turn import TurnEngine
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.guardrails.crisis import CrisisHandler
from eidolon_agent.domain.guardrails.input_filter import InputGuardrail
from eidolon_agent.domain.guardrails.output_filter import OutputGuardrail
from eidolon_agent.domain.harness.realtime import HarnessBudget, RealtimeAgentHarness
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.domain.history.fanout import HistoryFanout
from eidolon_agent.domain.tools.dispatcher import ToolDispatcher
from eidolon_agent.domain.tools.registry import ToolRegistry


class MemoryCondition:
    """Fault injection at the Memory port, independent from real Memory E2E tests."""

    state = "empty"

    async def recall_context(self, **kwargs):
        if self.state == "timeout":
            raise TimeoutError("injected recall timeout")
        if self.state == "unavailable":
            raise ConnectionError("injected memory disconnect")
        if self.state == "kg_only":
            return MemoryRecallResult(
                context="合成测试事实：用户喜欢茉莉花茶。", kg_triples=[{"id": "fixture"}]
            )
        return MemoryRecallResult()


class Conversation:
    def __init__(self, stack, facts, *, memory=None, model=None):
        self.facts = facts
        self.conversation_id = str(uuid4())
        self.history = HistoryManager()
        settings = load_settings()
        harness = RealtimeAgentHarness(
            budget=HarnessBudget(
                history_window=settings.turn.history_context_window,
                message_budget_tokens=settings.turn.max_token_budget,
                tool_schema_budget_tokens=settings.turn.tool_schema_budget_tokens,
                output_reserve_tokens=settings.turn.output_reserve_tokens,
            )
        )
        self.compiler = ContextCompiler(
            personas_service=stack.personas,
            instance_locator=lambda *_: (facts.companion_id, facts.genome_id),
            history_manager=self.history,
            memory_port=memory,
            history_window=harness.budget.history_window,
            degraded_history_window=settings.turn.degraded_history_context_window,
            memory_timeout_s=settings.memory.recall_timeout_s,
            context_budget_tokens=settings.turn.max_token_budget,
            context_budget_mode=settings.turn.context_budget_mode,
            harness=harness,
        )
        self.engine = TurnEngine(
            compiler=self.compiler,
            llm=model or stack.model,
            tool_dispatcher=ToolDispatcher(ToolRegistry()),
            history=self.history,
            fanout=HistoryFanout(),
            input_guardrail=InputGuardrail(),
            output_guardrail=OutputGuardrail(),
            crisis=CrisisHandler(),
            personas_service=stack.personas,
            genome_id=facts.genome_id,
            memory_write_mode="disabled",
            harness=harness,
        )

    def turn(self, text):
        identifier = str(uuid4())
        facts = self.facts
        return TurnInput(
            turn_id=identifier,
            conversation_id=self.conversation_id,
            session_id=self.conversation_id,
            input_modality="voice",
            trigger=TurnTrigger.USER_UTTERANCE,
            text=text,
            runtime_config=CompanionRuntimeConfig(temperature=0),
            metadata={
                "turn_decision": CommittedTurnDecision.create(
                    text=text, boundary=TurnCommitBoundary.PTT_SEGMENT
                ).as_metadata()
            },
            context=TurnContext(
                owner_id=facts.owner_id,
                companion_id=facts.companion_id,
                device_id=None,
                memory_realm_id=facts.memory_realm_id,
                genome_id=facts.genome_id,
                schema_version=facts.schema_version,
                genome_hash=facts.genome_hash,
                realizer_version=facts.realizer_version,
                trace_id=identifier,
                request_id=identifier,
            ),
        )

    async def say(self, text):
        self.last_turn = self.turn(text)
        events = [event async for event in self.engine.run(self.last_turn)]
        answer = "".join(
            event.data.get("text", "") for event in events if event.kind.value == "delta"
        )
        return answer, events
