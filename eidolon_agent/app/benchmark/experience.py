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

from eidolon_sdk.core.runtime import BackgroundTaskRunner
from eidolon_sdk.memory import conversation_turn_subject

from eidolon_agent.core.types.event import Event
from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.core.types.memory import MemoryRecallResult
from eidolon_agent.core.types.messages import ChatMessage, MessageRole
from eidolon_agent.core.types.turn import TurnEventKind, TurnInput, TurnTrigger
from eidolon_agent.domain.agent import TaskClassifier, TurnEngine
from eidolon_agent.domain.context import ContextCompiler
from eidolon_agent.domain.guardrails import CrisisHandler, InputGuardrail, OutputGuardrail
from eidolon_agent.domain.history import HistoryFanout, HistoryManager
from eidolon_agent.domain.tools import EmitEventTool, ToolDispatcher, ToolRegistry
from eidolon_agent.infra.events import InMemoryEventBus

SCHEMA_VERSION = "eidolon_agent.experience_replay_report.v1"
_REPLAY_TENANT_ID = "replay"
_REPLAY_USER_ID = "alice"
_REPLAY_AGENT_INSTANCE_ID = "inst-test"
_REPLAY_PERSONA_ID = "caretaker_jiezhi"


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
                    "structure_version": trace.get("context_structure_version"),
                    "history_presentation": trace.get("history_presentation"),
                    "tags": list(trace.get("context_tags") or []),
                    "interrupted_context_dropped_count": trace.get(
                        "interrupted_context_dropped_count"
                    )
                    or 0,
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
                "tools": {
                    "visible_names": list(
                        ((trace.get("harness") or {}).get("tools") or {}).get(
                            "visible_names"
                        )
                        or []
                    ),
                    "repeat_suppressed_count": trace.get(
                        "tool_repeat_suppressed_count"
                    )
                    or 0,
                },
            },
        }


@dataclass(slots=True)
class ScenarioReplayResult:
    scenario_id: str
    description: str
    turns: list[TurnReplayResult]
    category: str = "uncategorized"
    tags: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(t.passed for t in self.turns) and all(c["passed"] for c in self.checks)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "description": self.description,
            "category": self.category,
            "tags": list(self.tags),
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
            "metrics": _report_metrics(results),
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
            category=str(scenario.get("category") or "uncategorized"),
            tags=[str(tag) for tag in scenario.get("tags") or []],
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
        self.background_tasks = BackgroundTaskRunner(component="experience-replay")

    async def start(self) -> None:
        await self.event_bus.subscribe(
            conversation_turn_subject(
                f"{_REPLAY_TENANT_ID}.{_REPLAY_USER_ID}"
            ),
            self._on_memory_fanout,
        )
        now = datetime.now(timezone.utc)
        for idx, item in enumerate(self.scenario.get("history") or []):
            role_name = str(item.get("role") or "user").lower()
            role = MessageRole.ASSISTANT if role_name == "assistant" else MessageRole.USER
            await self.history.append(
                conversation_id=str(
                    item.get("conversation_id")
                    or self.scenario.get("conversation_id")
                    or "replay"
                ),
                message=ChatMessage(
                    id=str(item.get("id") or f"seed-{idx + 1}"),
                    role=role,
                    content=str(item.get("content") or ""),
                    created_at=now,
                    metadata=dict(item.get("metadata") or {}),
                ),
            )

    async def run_turn(self, spec: dict[str, Any], *, idx: int) -> TurnReplayResult:
        llm = _CapturingLLM(response_text=str(spec.get("assistant") or "我在。"))
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
        await self.background_tasks.drain(timeout_s=2.0)
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
        result.checks = _check_turn_expectations(
            _merge_expectations(
                self.scenario.get("default_turn_expect") or {},
                spec.get("expect") or {},
            ),
            result,
            self,
        )
        return result

    async def _on_memory_fanout(self, ev: Event) -> None:
        payload = dict(ev.payload)
        self.memory_fanouts.append(payload)
        turn_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
        disposition = (turn_payload.get("metadata") or {}).get("memory_write_disposition")
        user_text = str(turn_payload.get("user_text") or "")
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
            persona_template_id=_REPLAY_PERSONA_ID,
            memory_port=self.memory,
            turn_persister=self._capture_turn,
            background_tasks=self.background_tasks,
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

    def __init__(self, *, response_text: str) -> None:
        self.response_text = response_text
        self.messages: list[ChatMessage] = []
        self.prompt_text = ""

    async def stream(self, messages: list[ChatMessage], **_: Any):
        self.messages = list(messages)
        self.prompt_text = "\n\n".join(m.content for m in messages)
        yield LLMDelta(text_delta=self.response_text)
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
        self.forget_calls: list[dict[str, Any]] = []

    async def recall_context(self, **_: Any):
        if self.raise_on_recall:
            raise RuntimeError("memory backend down")
        hits = [SimpleNamespace(id="replay-memory-1")] if self.context else []
        return MemoryRecallResult(
            context=self.context,
            hits=hits,
            degraded=self.degraded,
        )

    async def forget(
        self,
        owner_id: str | None,
        companion_id: str | None,
        memory_realm_id: str,
        device_id: str | None,
        query: str,
        *,
        session_id: str | None = None,
    ) -> int:
        self.forget_calls.append(
            {
                "owner_id": owner_id,
                "companion_id": companion_id,
                "memory_realm_id": memory_realm_id,
                "device_id": device_id,
                "query": query,
                "session_id": session_id,
            }
        )
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


async def run_replay_scenarios(
    scenarios: Iterable[dict[str, Any]],
    *,
    memory_report_path: Path | None = None,
) -> dict[str, Any]:
    memory_report = None
    if memory_report_path is not None:
        memory_report = json.loads(memory_report_path.read_text(encoding="utf-8"))
    runner = ExperienceReplayRunner()
    return await runner.run_many(list(scenarios), memory_report=memory_report)


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
    for item in expect.get("required_assistant_substrings") or []:
        add(f"required_assistant:{item}", item in result.assistant_text)
    for item in expect.get("forbidden_assistant_substrings") or []:
        add(f"forbidden_assistant:{item}", item not in result.assistant_text)
    for item in expect.get("required_memory_context_substrings") or []:
        add(f"required_memory_context:{item}", item in harness.memory.context)
    for item in expect.get("forbidden_memory_context_substrings") or []:
        add(f"forbidden_memory_context:{item}", item not in harness.memory.context)
    trace = result.turn_trace or {}
    write = trace.get("memory_write_trace") or {}
    recall = trace.get("memory_trace") or {}
    context_tags = trace.get("context_tags") or []
    tool_call_names = [
        str((event.get("data") or {}).get("name"))
        for event in result.events
        if event.get("kind") == TurnEventKind.TOOL_CALL.value
    ]
    if expect.get("no_tool_calls"):
        add("no_tool_calls", not tool_call_names, f"got={tool_call_names}")
    if expect.get("current_request_authority"):
        add(
            "current_request_authority",
            "[CURRENT REQUEST]" in result.prompt_text
            and any(
                isinstance(tag, dict)
                and tag.get("kind") == "current_user"
                and tag.get("authority") == "current_request"
                and tag.get("actionability") == "may_execute"
                for tag in context_tags
            ),
        )
    if expect.get("background_non_actionable"):
        add(
            "background_non_actionable",
            "[BACKGROUND CONTEXT]" in result.prompt_text
            and any(
                isinstance(tag, dict)
                and tag.get("kind") == "history"
                and tag.get("authority") == "background"
                and tag.get("actionability") == "must_not_execute"
                for tag in context_tags
            ),
        )
    for name in expect.get("forbidden_tool_names") or []:
        add(f"forbidden_tool:{name}", str(name) not in tool_call_names)
    if "context_structure_version" in expect:
        add(
            "context_structure_version",
            trace.get("context_structure_version") == expect["context_structure_version"],
            f"got={trace.get('context_structure_version')}",
        )
    if "history_presentation" in expect:
        add(
            "history_presentation",
            trace.get("history_presentation") == expect["history_presentation"],
            f"got={trace.get('history_presentation')}",
        )
    for required in expect.get("required_context_tags") or []:
        required_items = dict(required)
        add(
            f"required_context_tag:{required_items}",
            any(
                all(tag.get(key) == value for key, value in required_items.items())
                for tag in context_tags
                if isinstance(tag, dict)
            ),
        )
    if "max_tool_repeat_suppressed" in expect:
        actual = int(trace.get("tool_repeat_suppressed_count") or 0)
        add(
            "max_tool_repeat_suppressed",
            actual <= int(expect["max_tool_repeat_suppressed"]),
            f"got={actual}",
        )
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


def _merge_expectations(
    base: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    if override.get("skip_default_expect"):
        return {
            key: value
            for key, value in override.items()
            if key != "skip_default_expect"
        }
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = [*merged[key], *value]
        else:
            merged[key] = value
    return merged


def _report_metrics(results: list[ScenarioReplayResult]) -> dict[str, Any]:
    turns = [turn for scenario in results for turn in scenario.turns]
    checks = [
        check
        for scenario in results
        for turn in scenario.turns
        for check in turn.checks
        if not check.get("skipped")
    ]
    categories: dict[str, dict[str, int]] = {}
    for scenario in results:
        bucket = categories.setdefault(
            scenario.category,
            {"scenario_count": 0, "passed": 0, "failed": 0, "turn_count": 0},
        )
        bucket["scenario_count"] += 1
        bucket["turn_count"] += len(scenario.turns)
        if scenario.passed:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
    return {
        "turn_count": len(turns),
        "check_count": len(checks),
        "check_pass_rate": _ratio(
            sum(1 for check in checks if bool(check.get("passed"))),
            len(checks),
        ),
        "scenario_pass_rate": _ratio(
            sum(1 for scenario in results if scenario.passed),
            len(results),
        ),
        "first_delta_ms": _latency_summary(
            turn.first_delta_ms for turn in turns if turn.first_delta_ms is not None
        ),
        "total_ms": _latency_summary(
            turn.total_ms for turn in turns if turn.total_ms is not None
        ),
        "categories": categories,
    }


def _latency_summary(values: Iterable[int]) -> dict[str, int | None]:
    ordered = sorted(int(value) for value in values)
    if not ordered:
        return {"p50": None, "p95": None, "max": None}
    return {
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "max": ordered[-1],
    }


def _percentile(values: list[int], percentile: float) -> int:
    idx = min(len(values) - 1, max(0, round((len(values) - 1) * percentile)))
    return values[idx]


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 1.0
    return round(numerator / denominator, 4)


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
                owner_id=_REPLAY_USER_ID,
                companion_id=_REPLAY_AGENT_INSTANCE_ID,
                device_id=None,
                memory_realm_id=f"{_REPLAY_TENANT_ID}.{_REPLAY_USER_ID}",
                genome_id=_REPLAY_PERSONA_ID,
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
