from __future__ import annotations

import asyncio
import time

import pytest
from eidolon_sdk.biz.interpretation import (
    ERROR_INVALID_PROPOSAL,
    ERROR_TIMEOUT,
    ERROR_UNAVAILABLE,
    Action,
    Candidate,
    InterpretationError,
    InterpretationRequest,
    InterpretationResult,
    Proposal,
)

from eidolon_agent.core.ports.interpretation import InteractionInterpretationPort
from eidolon_agent.core.types.interpretation import RUN_EXCEPTION
from eidolon_agent.domain.interpretation import InterpretationConfig, InterpretationService
from eidolon_agent.infra.interpretation import InMemoryInterpretationRecorder

pytestmark = pytest.mark.unit

REQUEST = InterpretationRequest(
    interpretation_id="turn-1",
    domain="smarthome",
    utterance="打开空调",
    candidates=(Candidate(ref="living.ac", name="客厅空调", kind="climate"),),
    timeout_ms=200,
)


def _result(targets: tuple[str, ...] = ("living.ac",), *, model: str = "m") -> InterpretationResult:
    return InterpretationResult(
        interpretation_id=REQUEST.interpretation_id,
        status="decided",
        proposal=Proposal(
            intent="control",
            target_status="resolved",
            targets=targets,
            action=Action(trait="on_off", command="on"),
        ),
        policy_version="p",
        model_version=model,
    )


class _Adapter:
    def __init__(self, answer=None, *, delay_s: float = 0.0) -> None:
        self.answer = answer if answer is not None else _result()
        self.delay_s = delay_s
        self.calls = 0

    async def interpret(self, request: InterpretationRequest) -> InterpretationResult:
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class _BrokenRecorder:
    async def record(self, record) -> None:
        raise OSError("disk full")


def _service(primary, *shadows, recorder=None, shadow_timeout_s: float = 0.3):
    adapters = {"primary": primary} | {f"shadow{i}": s for i, s in enumerate(shadows)}
    config = InterpretationConfig(
        primary="primary",
        shadows=tuple(f"shadow{i}" for i in range(len(shadows))),
        shadow_timeout_s=shadow_timeout_s,
    )
    return InterpretationService(adapters, config, recorder=recorder)


async def test_primary_answers_and_everything_is_recorded() -> None:
    recorder = InMemoryInterpretationRecorder()
    shadow = _Adapter(_result(model="shadow"))
    service = _service(_Adapter(), shadow, recorder=recorder)

    result = await service.interpret(REQUEST)
    await service.drain()

    assert result == _result()
    assert isinstance(service, InteractionInterpretationPort)
    (record,) = recorder.records
    assert record.request == REQUEST
    assert (record.primary.adapter, record.primary.role, record.primary.result) == (
        "primary",
        "primary",
        _result(),
    )
    assert record.primary.latency_ms >= 0
    assert [(s.adapter, s.result.model_version) for s in record.shadows] == [("shadow0", "shadow")]


async def test_slow_or_failing_shadows_never_delay_or_change_the_primary() -> None:
    recorder = InMemoryInterpretationRecorder()
    slow = _Adapter(delay_s=5.0)
    crashing = _Adapter(RuntimeError("boom"))
    refusing = _Adapter(InterpretationError(ERROR_UNAVAILABLE, "down"))
    different = _Adapter(
        InterpretationResult(
            interpretation_id="turn-1", status="abstained", policy_version="p", model_version="x"
        )
    )
    service = _service(_Adapter(), slow, crashing, refusing, different, recorder=recorder)

    started = time.monotonic()
    result = await service.interpret(REQUEST)
    elapsed = time.monotonic() - started
    await service.drain()

    assert elapsed < 0.2
    assert result == _result()
    shadows = {s.adapter: s for s in recorder.records[0].shadows}
    assert shadows["shadow0"].error == ERROR_TIMEOUT
    assert shadows["shadow1"].error == RUN_EXCEPTION
    assert shadows["shadow2"].error == ERROR_UNAVAILABLE
    assert shadows["shadow3"].result.status == "abstained"


async def test_invalid_primary_proposal_is_abstained_and_recorded_raw() -> None:
    recorder = InMemoryInterpretationRecorder()
    outside = _result(targets=("garage.door",))
    service = _service(_Adapter(outside), recorder=recorder)

    result = await service.interpret(REQUEST)
    await service.drain()

    assert result.status == "abstained"
    assert result.proposal is None
    assert result.diagnostics == {"rejected": "TARGET_OUTSIDE_CANDIDATES"}
    primary = recorder.records[0].primary
    assert (primary.result, primary.rejected) == (outside, "TARGET_OUTSIDE_CANDIDATES")


async def test_invalid_shadow_proposal_is_recorded_as_rejected() -> None:
    recorder = InMemoryInterpretationRecorder()
    service = _service(_Adapter(), _Adapter(_result(targets=("x",))), recorder=recorder)

    await service.interpret(REQUEST)
    await service.drain()

    assert recorder.records[0].shadows[0].rejected == "TARGET_OUTSIDE_CANDIDATES"


@pytest.mark.parametrize(
    ("answer", "delay_s", "code"),
    [
        (InterpretationError(ERROR_UNAVAILABLE, "down"), 0.0, ERROR_UNAVAILABLE),
        (None, 1.0, ERROR_TIMEOUT),  # exceeds the request's 200 ms
        (RuntimeError("bug"), 0.0, ERROR_UNAVAILABLE),
        ("not a result", 0.0, ERROR_INVALID_PROPOSAL),
    ],
)
async def test_primary_failures_raise_and_are_recorded(answer, delay_s, code) -> None:
    recorder = InMemoryInterpretationRecorder()
    service = _service(_Adapter(answer, delay_s=delay_s), recorder=recorder)

    with pytest.raises(InterpretationError) as raised:
        await service.interpret(REQUEST)
    await service.drain()

    assert raised.value.code == code
    assert recorder.records[0].primary.error is not None


async def test_recorder_failure_does_not_fail_the_primary() -> None:
    service = _service(_Adapter(), recorder=_BrokenRecorder())

    assert await service.interpret(REQUEST) == _result()
    await service.drain()


async def test_cancelled_primary_is_still_recorded() -> None:
    recorder = InMemoryInterpretationRecorder()
    service = _service(_Adapter(delay_s=5.0), recorder=recorder)
    task = asyncio.create_task(service.interpret(REQUEST))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await service.drain()

    assert recorder.records[0].primary.error == "CANCELLED"


def test_config_must_name_registered_distinct_adapters() -> None:
    adapters = {"rules": _Adapter(), "laya": _Adapter()}
    with pytest.raises(ValueError, match="not registered"):
        InterpretationService(adapters, InterpretationConfig(primary="llm"))
    with pytest.raises(ValueError, match="twice"):
        InterpretationService(adapters, InterpretationConfig(primary="rules", shadows=("rules",)))
    promoted = InterpretationService(
        adapters, InterpretationConfig(primary="laya", shadows=("rules",))
    )
    assert promoted.primary == "laya"
