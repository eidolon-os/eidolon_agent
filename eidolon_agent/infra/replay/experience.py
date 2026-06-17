"""In-process experience replay runner.

The runner validates product-facing behaviors without real NATS, LLM, or
memory services. It is intentionally prompt-safe in its persisted report:
checks can inspect captured prompts locally, but report artifacts only carry
turn ids, check names, latency, and trace summaries.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from eidolon_sdk.memory import conversation_turn_subject

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.messages import ChatMessage
from eidolon_agent.core.types.turn import TurnEventKind, TurnInput, TurnTrigger
from eidolon_agent.domain.agent import TaskClassifier, TurnEngine
from eidolon_agent.domain.context import ContextCompiler
from eidolon_agent.domain.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.domain.history import HistoryFanout, HistoryManager
from eidolon_agent.domain.tools import EmitEventTool, ToolDispatcher, ToolRegistry
from eidolon_agent.infra.events import InMemoryEventBus

SCHEMA_VERSION = "eidolon_agent.experience_replay_report.v1"


@dataclass(slots=True)
class TurnReplayResult:
    turn_id: str
    input_text: str
    prompt_text: str = ""
    assistant_text: str = ""
    first_delta_ms: int | None = None
    total_ms: int | None = None
    turn_trace: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    memory_fanouts: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c["passed"] for c in self.checks)

    def to_metadata(self) -> dict[str, Any]:
        trace = self.turn_trace or {}
        return {
            "turn_id": self.turn_id,
            "passed": self.passed,
            "first_delta_ms": self.first_delta_ms,
            "total_ms": self.total_ms,
            "checks": self.checks,
            "memory_fanout_count": len(self.memory_fanouts),
            "trace_summary": {
                "privacy": (trace.get("privacy") or {}).get("mode"),
                "memory_write": {
                    "disposition": (trace.get("memory_write_trace") or {}).get(
                        "disposition"
                    ),
                    "fanout_allowed": (trace.get("memory_write_trace") or {}).get(
                        "fanout_allowed"
                    ),
                    "skipped_reason": (trace.get("memory_write_trace") or {}).get(
                        "skipped_reason"
                    ),
                },
                "memory_recall": {
                    "attempted": (trace.get("memory_trace") or {}).get("attempted"),
                    "degraded": (trace.get("memory_trace") or {}).get("degraded"),
                    "degraded_reason": (trace.get("memory_trace") or {}).get(
                        "degraded_reason"
                    ),
                    "context_injected": (trace.get("memory_trace") or {}).get(
                        "context_injected"
                    ),
                    "kg_triple_count": (trace.get("memory_trace") or {}).get(
                        "kg_triple_count"
                    ),
                },
                "context": {
                    "segments": [
                        s.get("kind")
                        for s in (trace.get("context_ledger") or {}).get("segments", [])
                    ],
                    "dropped": [
                        s.get("kind")
                        for s in (trace.get("context_ledger") or {}).get(
                            "dropped_segments", []
                        )
                    ],
                    "degraded_sources": list(
                        (trace.get("context_ledger") or {}).get("degraded_sources")
                        or []
                    ),
                },
            },
        }


@dataclass(slots=True)
class ScenarioReplayResult:
    scenario_id: str
    description: str
    turns: list[TurnReplayResult]
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.turns) and all(c["passed"] for c in self.checks)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "description": self.description,
            "passed": self.passed,
            "turns": [t.to_metadata() for t in self.turns],
            "checks": self.checks,
        }


class ExperienceReplayRunner:
    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now or datetime.now(timezone.utc)

    async def run_many(
        self,
        scenarios: Iterable[dict[str, Any]],
        *,
        memory_report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        results = [await self.run_scenario(s) for s in scenarios]
        report = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": self._now.isoformat(),
            "passed": all(r.passed for r in results),
            "summary": {
                "scenario_count": len(results),
                "passed": sum(1 for r in results if r.passed),
                "failed": sum(1 for r in results if not r.passed),
            },
            "scenarios": [r.to_metadata() for r in results],
        }
        if memory_report is not None:
            report["memory_quality"] = _memory_quality_summary(memory_report)
        return report

    async def run_scenario(self, scenario: dict[str, Any]) -> ScenarioReplayResult:
        harness = _ReplayHarness(scenario)
        await harness.start()
        turn_results: list[TurnReplayResult] = []
        for idx, turn in enumerate(scenario.get("turns") or []):
            turn_results.append(await harness.run_turn(turn, idx=idx))
        scenario_checks = _check_scenario_expectations(
            scenario.get("expect") or {},
            turn_results=turn_results,
            harness=harness,
        )
        return ScenarioReplayResult(
            scenario_id=str(scenario.get("id") or uuid.uuid4().hex),
            description=str(scenario.get("description") or ""),
            turns=turn_results,
            checks=scenario_checks,
        )


class _ReplayHarness:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.event_bus = InMemoryEventBus()
        self.history = HistoryManager()
        self.memory = _ReplayMemory(
            context=(scenario.get("memory") or {}).get("initial_context") or "",
            degraded=bool((scenario.get("memory") or {}).get("degraded")),
            raise_on_recall=bool((scenario.get("memory") or {}).get("raise_on_recall")),
        )
        self.memory_fanouts: list[dict[str, Any]] = []
        self.captured_traces: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        await self.event_bus.subscribe(
            conversation_turn_subject("alice"),
            self._on_memory_fanout,
        )

    async def run_turn(self, spec: dict[str, Any], *, idx: int) -> TurnReplayResult:
        llm = _CapturingLLM()
        engine = self._build_engine(llm=llm)
        turn_id = str(spec.get("turn_id") or f"{self.scenario.get('id')}-{idx + 1}")
        ti = _make_turn_input(
            text=str(spec.get("user") or ""),
            turn_id=turn_id,
            conversation_id=str(
                spec.get("conversation_id")
                or self.scenario.get("conversation_id")
                or "replay"
            ),
            metadata=dict(spec.get("metadata") or {}),
        )

        t0 = time.monotonic()
        events = []
        assistant_parts: list[str] = []
        first_delta_ms: int | None = None
        total_ms: int | None = None
        async for ev in engine.run(ti):
            events.append({"kind": ev.kind.value, "data": dict(ev.data)})
            if ev.kind is TurnEventKind.DELTA:
                if first_delta_ms is None:
                    first_delta_ms = int((time.monotonic() - t0) * 1000)
                assistant_parts.append(str(ev.data.get("text") or ""))
            elif ev.kind in {TurnEventKind.DONE, TurnEventKind.ERROR}:
                total_ms = int((time.monotonic() - t0) * 1000)
        await _drain_background_tasks()

        result = TurnReplayResult(
            turn_id=turn_id,
            input_text=ti.text or "",
            prompt_text=llm.prompt_text,
            assistant_text="".join(assistant_parts),
            first_delta_ms=first_delta_ms,
            total_ms=total_ms,
            turn_trace=self.captured_traces.get(turn_id),
            events=events,
            memory_fanouts=list(self.memory_fanouts),
        )
        result.checks = _check_turn_expectations(spec.get("expect") or {}, result, self)
        return result

    async def _on_memory_fanout(self, ev: Event) -> None:
        payload = dict(ev.payload)
        self.memory_fanouts.append(payload)
        disposition = (payload.get("metadata") or {}).get("memory_write_disposition")
        user_text = str(payload.get("user_text") or "")
        if disposition == "semantic_upsert":
            self.memory.context = _semantic_context_from_text(user_text)
        elif disposition == "promise_create":
            self.memory.context = "强制承诺: 明天提醒用户喝水"

    def _build_engine(self, *, llm: _CapturingLLM) -> TurnEngine:
        registry = ToolRegistry()
        registry.register(EmitEventTool(event_bus=self.event_bus))
        compiler = ContextCompiler(
            personas_service=_ReplayPersonas(),
            instance_locator=lambda _t, _u, _c: ("inst-test", "caretaker_jiezhi"),
            history_manager=self.history,
            memory_port=self.memory,
            context_budget_tokens=(self.scenario.get("context") or {}).get("budget_tokens"),
            context_budget_mode=(self.scenario.get("context") or {}).get(
                "budget_mode", "enabled"
            ),
        )
        return TurnEngine(
            compiler=compiler,
            llm=llm,
            tool_dispatcher=ToolDispatcher(registry),
            history=self.history,
            fanout=HistoryFanout(event_bus=self.event_bus),
            triage=TaskClassifier(),
            input_guardrail=InputGuardrail(),
            output_guardrail=OutputGuardrail(),
            crisis=CrisisHandler(event_bus=self.event_bus),
            event_bus=self.event_bus,
            personas_service=_ReplayPersonas(),
            persona_template_id="caretaker_jiezhi",
            memory_port=self.memory,
            turn_persister=self._capture_turn,
        )

    async def _capture_turn(self, **kwargs: Any) -> None:
        timings = kwargs.get("timings") or {}
        ti = kwargs.get("ti")
        if ti is not None and timings.get("turn_trace"):
            self.captured_traces[ti.turn_id] = timings["turn_trace"]


class _ReplayPersonas:
    async def compile_prompt(self, **_: Any) -> SimpleNamespace:
        return SimpleNamespace(
            system_prompt="[PERSONA]\n你是一个稳定、诚实、尊重隐私的智能陪伴体。",
            debug_trace=(),
        )

    async def submit_interaction(self, *_: Any, **__: Any) -> None:
        return None


class _CapturingLLM:
    model_id = "fake:experience-replay"

    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []
        self.prompt_text = ""

    async def stream(self, messages: list[ChatMessage], **_: Any):
        self.messages = list(messages)
        self.prompt_text = "\n\n".join(m.content for m in messages)
        yield LLMDelta(text_delta="我在。")
        yield LLMDelta(finish=LLMFinishReason.STOP)

    async def count_tokens(self, messages: list[ChatMessage]) -> int:
        return sum(max(1, len(m.content) // 3) for m in messages)


class _ReplayMemory:
    def __init__(
        self,
        *,
        context: str,
        degraded: bool = False,
        raise_on_recall: bool = False,
    ) -> None:
        self.context = context
        self.degraded = degraded
        self.raise_on_recall = raise_on_recall
        self.forget_calls: list[tuple[str, str]] = []

    async def recall_context(self, **_: Any):
        if self.raise_on_recall:
            raise RuntimeError("memory backend down")
        hits = [SimpleNamespace(id="replay-memory-1")] if self.context else []
        return self.context, hits, self.degraded

    async def forget(self, user_id: str, query: str) -> int:
        self.forget_calls.append((user_id, query))
        removed = 1 if self.context else 0
        self.context = ""
        return removed


def load_replay_scenarios(paths: Iterable[Path]) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                scenario = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            scenario.setdefault("id", f"{path.stem}:{lineno}")
            scenarios.append(scenario)
    return scenarios


async def run_replay_files(
    paths: Iterable[Path],
    *,
    memory_report_path: Path | None = None,
) -> dict[str, Any]:
    memory_report = None
    if memory_report_path is not None:
        memory_report = json.loads(memory_report_path.read_text(encoding="utf-8"))
    runner = ExperienceReplayRunner()
    return await runner.run_many(
        load_replay_scenarios(paths),
        memory_report=memory_report,
    )


def _check_turn_expectations(
    expect: dict[str, Any],
    result: TurnReplayResult,
    harness: _ReplayHarness,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    for item in expect.get("required_prompt_substrings") or []:
        add(f"required_prompt:{item}", item in result.prompt_text)
    for item in expect.get("forbidden_prompt_substrings") or []:
        add(f"forbidden_prompt:{item}", item not in result.prompt_text)
    for item in expect.get("required_memory_context_substrings") or []:
        add(f"required_memory_context:{item}", item in harness.memory.context)
    for item in expect.get("forbidden_memory_context_substrings") or []:
        add(f"forbidden_memory_context:{item}", item not in harness.memory.context)
    trace = result.turn_trace or {}
    write = trace.get("memory_write_trace") or {}
    recall = trace.get("memory_trace") or {}
    if "memory_write_disposition" in expect:
        add(
            "memory_write_disposition",
            write.get("disposition") == expect["memory_write_disposition"],
            f"got={write.get('disposition')}",
        )
    if "memory_fanout_allowed" in expect:
        add(
            "memory_fanout_allowed",
            bool(write.get("fanout_allowed")) is bool(expect["memory_fanout_allowed"]),
            f"got={write.get('fanout_allowed')}",
        )
    if "memory_recall_degraded" in expect:
        add(
            "memory_recall_degraded",
            bool(recall.get("degraded")) is bool(expect["memory_recall_degraded"]),
            f"got={recall.get('degraded')}",
        )
    if expect.get("forget_called"):
        add("forget_called", bool(harness.memory.forget_calls))
    if expect.get("memory_context_empty"):
        add("memory_context_empty", harness.memory.context == "")
    if "max_first_delta_ms" in expect:
        actual = result.first_delta_ms
        add(
            "max_first_delta_ms",
            actual is not None and actual <= int(expect["max_first_delta_ms"]),
            f"got={actual}",
        )
    if "max_total_ms" in expect:
        actual = result.total_ms
        add(
            "max_total_ms",
            actual is not None and actual <= int(expect["max_total_ms"]),
            f"got={actual}",
        )
    return checks


def _check_scenario_expectations(
    expect: dict[str, Any],
    *,
    turn_results: list[TurnReplayResult],
    harness: _ReplayHarness,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    full_prompt = "\n\n".join(t.prompt_text for t in turn_results)
    for item in expect.get("forbidden_prompt_substrings") or []:
        add(f"scenario_forbidden_prompt:{item}", item not in full_prompt)
    if expect.get("memory_context_empty"):
        add("scenario_memory_context_empty", harness.memory.context == "")
    if expect.get("forget_called"):
        add("scenario_forget_called", bool(harness.memory.forget_calls))
    return checks


def _semantic_context_from_text(text: str) -> str:
    if "阿满" in text:
        return "称呼偏好: 用户希望被叫作阿满"
    if "小满" in text:
        return "称呼偏好: 用户希望被叫作小满"
    if "杭州" in text:
        return "事实更正: 用户现在住在杭州"
    if "上海" in text:
        return "事实: 用户住在上海"
    return f"用户偏好: {text}"


def _make_turn_input(
    *,
    text: str,
    turn_id: str,
    conversation_id: str,
    metadata: dict[str, Any],
) -> TurnInput:
    return TurnInput(
        turn_id=turn_id,
        conversation_id=conversation_id,
        session_id="replay-session",
        caller=CallerContext(
            identity=Identity(
                tenant_id="replay",
                user_id="alice",
                agent_instance_id="inst-test",
            ),
            caller_kind=CallerKind.WEB_CHAT,
            trace_id=f"replay-{turn_id}",
            request_id=f"replay-{turn_id}",
        ),
        trigger=TurnTrigger.USER_UTTERANCE,
        text=text,
        metadata=metadata,
    )


def _memory_quality_summary(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary") if isinstance(report, dict) else None
    if isinstance(summary, dict):
        return summary
    keys = {
        key: report.get(key)
        for key in ("passed", "precision", "recall", "mrr", "latency_p95_ms")
        if key in report
    }
    return keys or {"attached": True}


async def _drain_background_tasks() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
