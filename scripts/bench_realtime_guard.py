"""Run in-process realtime guard checks and emit a compact report."""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path

from eidolon_agent.app.replay import load_replay_scenarios
from eidolon_agent.app.replay.experience import ExperienceReplayRunner


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        action="append",
        type=Path,
        default=[],
        help="Replay fixture to benchmark. Defaults to core experience fixture.",
    )
    parser.add_argument("--first-delta-p95-ms", type=int, default=300)
    parser.add_argument("--total-p95-ms", type=int, default=500)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    fixtures = args.fixture or [Path("tests/replay/fixtures/core_experience.jsonl")]
    scenarios = load_replay_scenarios(fixtures)
    report = await ExperienceReplayRunner().run_many(scenarios)
    turn_rows = [
        turn
        for scenario in report["scenarios"]
        for turn in scenario["turns"]
    ]
    first = [t["first_delta_ms"] for t in turn_rows if t["first_delta_ms"] is not None]
    total = [t["total_ms"] for t in turn_rows if t["total_ms"] is not None]
    first_p95 = _p95(first)
    total_p95 = _p95(total)
    guard = {
        "schema_version": "eidolon_agent.realtime_guard_report.v1",
        "passed": (
            report["passed"]
            and first_p95 is not None
            and first_p95 <= args.first_delta_p95_ms
            and total_p95 is not None
            and total_p95 <= args.total_p95_ms
        ),
        "thresholds": {
            "first_delta_p95_ms": args.first_delta_p95_ms,
            "total_p95_ms": args.total_p95_ms,
        },
        "metrics": {
            "turn_count": len(turn_rows),
            "first_delta_p50_ms": _median(first),
            "first_delta_p95_ms": first_p95,
            "total_p50_ms": _median(total),
            "total_p95_ms": total_p95,
        },
        "replay_summary": report["summary"],
    }
    text = json.dumps(guard, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote realtime guard report to {args.output}")
    else:
        print(text)
    return 0 if guard["passed"] else 1


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    return int(statistics.median(values))


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))
    return ordered[idx]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
