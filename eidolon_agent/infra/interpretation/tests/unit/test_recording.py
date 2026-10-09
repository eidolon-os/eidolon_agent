from __future__ import annotations

import json

import pytest
from eidolon_sdk.biz.interpretation import (
    Candidate,
    InterpretationRequest,
    InterpretationResult,
)

from eidolon_agent.core.types.interpretation import AdapterRun, InterpretationRecord
from eidolon_agent.infra.interpretation import (
    InMemoryInterpretationRecorder,
    JsonlInterpretationRecorder,
    RulesInterpreter,
    replay_requests,
)

pytestmark = pytest.mark.unit


def _record(n: int) -> InterpretationRecord:
    request = InterpretationRequest(
        interpretation_id=f"turn-{n}",
        domain="smarthome",
        utterance="打开空调",
        candidates=(Candidate(ref="living.ac", name="客厅空调", kind="climate"),),
        timeout_ms=500,
    )
    result = InterpretationResult(
        interpretation_id=request.interpretation_id,
        status="abstained",
        policy_version="p",
        model_version="m",
    )
    return InterpretationRecord(
        recorded_at="2026-09-26T00:00:00+00:00",
        request=request,
        primary=AdapterRun("rules", "primary", 1.5, result=result),
        shadows=(AdapterRun("laya", "shadow", 80.0, error="TIMEOUT", detail="shadow deadline"),),
    )


async def test_jsonl_round_trip_replays_the_requests(tmp_path) -> None:
    path = tmp_path / "records" / "interpretations.jsonl"
    recorder = JsonlInterpretationRecorder(path)

    await recorder.record(_record(1))
    await recorder.record(_record(2))

    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["request"]["utterance"] == "打开空调"  # UTF-8, not \\u escapes
    assert "打开空调" in lines[0]
    assert first["primary"] == {
        "adapter": "rules",
        "role": "primary",
        "latency_ms": 1.5,
        "result": _record(1).primary.result.model_dump(mode="json"),
        "error": None,
        "detail": None,
        "rejected": None,
    }
    assert first["shadows"][0]["error"] == "TIMEOUT"

    replayed = list(replay_requests(path))
    assert [r.interpretation_id for r in replayed] == ["turn-1", "turn-2"]
    assert (await RulesInterpreter().interpret(replayed[0])).status == "decided"


async def test_in_memory_keeps_the_most_recent() -> None:
    recorder = InMemoryInterpretationRecorder(max_records=2)
    for n in range(3):
        await recorder.record(_record(n))

    assert [r.request.interpretation_id for r in recorder.records] == ["turn-1", "turn-2"]


async def test_replay_rotation_is_bounded_and_keeps_complete_utf8_records(tmp_path):
    import stat
    path = tmp_path/'private'/'replay.jsonl'
    size = len((json.dumps(_record(0).to_json(),ensure_ascii=False,separators=(',',':'))+'\n').encode())
    recorder = JsonlInterpretationRecorder(path, max_bytes=size+4, backups=2)
    for i in range(6):
        await recorder.record(_record(i))
    files = sorted(path.parent.iterdir())
    assert len(files)==3
    assert {json.loads(f.read_text())['request']['interpretation_id'] for f in files}=={'turn-3','turn-4','turn-5'}
    assert all(stat.S_IMODE(f.stat().st_mode)==0o600 for f in files)
    assert all(f.stat().st_size<=size+4 for f in files)


async def test_record_path_expands_host_state_root(tmp_path,monkeypatch):
    monkeypatch.setenv('EIDOLON_STATE_ROOT',str(tmp_path))
    recorder=JsonlInterpretationRecorder('$EIDOLON_STATE_ROOT/agent/replay.jsonl')
    await recorder.record(_record(0))
    assert recorder.path==tmp_path/'agent/replay.jsonl'


async def test_oversized_record_does_not_break_retention_limit(tmp_path):
    recorder=JsonlInterpretationRecorder(tmp_path/'replay.jsonl',max_bytes=10)
    with pytest.raises(ValueError,match='exceeds'):
        await recorder.record(_record(0))
    assert not recorder.path.exists()
