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

    Single-process size rotation bounds local replay data to four 8 MiB files.
    """

    def __init__(self, path: str | Path, *, max_bytes: int = 8 * 1024 * 1024, backups: int = 3) -> None:
        if max_bytes < 1 or backups < 0:
            raise ValueError("invalid replay retention bounds")
        self._path = Path(os.path.expandvars(str(path))).expanduser()
        self._max_bytes, self._backups = max_bytes, backups
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    async def record(self, record: InterpretationRecord) -> None:
        line = json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":"))
        async with self._lock:
            await asyncio.to_thread(self._append, line)

    def _append(self, line: str) -> None:
        data = (line + "\n").encode("utf-8")
        if len(data) > self._max_bytes:
            raise ValueError("interpretation record exceeds replay file limit")
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._path.exists() and self._path.stat().st_size + len(data) > self._max_bytes:
            for n in range(self._backups, 0, -1):
                src = self._path if n == 1 else Path(f"{self._path}.{n - 1}")
                if src.exists():
                    src.replace(Path(f"{self._path}.{n}"))
            if not self._backups:
                self._path.unlink()
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(data)


def replay_requests(path: str | Path) -> Iterator[InterpretationRequest]:
    """Yield the recorded requests, in order, to feed another interpreter."""
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield InterpretationRequest.model_validate(json.loads(line)["request"])
