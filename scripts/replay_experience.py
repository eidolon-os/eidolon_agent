"""Run in-process product experience replay fixtures.

Examples:
    python scripts/replay_experience.py
    python scripts/replay_experience.py --fixture tests/replay/fixtures/core.jsonl
    python scripts/replay_experience.py --memory-report ../eidolon_memory/reports/latest.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eidolon_agent.infra.replay import run_replay_files


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        action="append",
        type=Path,
        default=[],
        help="JSONL scenario fixture. May be passed more than once.",
    )
    parser.add_argument("--memory-report", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    fixtures = args.fixture or [Path("tests/replay/fixtures/core_experience.jsonl")]
    report = await run_replay_files(fixtures, memory_report_path=args.memory_report)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote replay report to {args.output}")
    else:
        print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
