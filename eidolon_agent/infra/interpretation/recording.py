"""Interpretation recorders: in memory (tests, dev) and append-only JSONL (replay)."""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from collections.abc import Iterator
from pathlib import Path

from eidolon_sdk.biz.interpretation import InterpretationRequest

from eidolon_agent.core.types.interpretation import InterpretationRecord


class InMemoryInterpretationRecorder:
    """Keeps the most recent records; the oldest fall off past ``max_records``."""

    def __init__(self, *, max_records: int = 1000) -> None:
        self._records: deque[InterpretationRecord] = deque(maxlen=max(1, max_records))

    async def record(self, record: InterpretationRecord) -> None:
        self._records.append(record)

    @property
    def records(self) -> tuple[InterpretationRecord, ...]:
        return tuple(self._records)


class JsonlInterpretationRecorder:
    """One JSON object per line. Writes run off the event loop, one at a time.

    Rotation and retention belong to deployment; this only appends.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def record(self, record: InterpretationRecord) -> None:
        line = json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def replay_requests(path: str | Path) -> Iterator[InterpretationRequest]:
    """Yield the recorded requests, in order, to feed another interpreter."""
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield InterpretationRequest.model_validate(json.loads(line)["request"])
