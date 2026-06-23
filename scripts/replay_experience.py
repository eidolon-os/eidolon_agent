"""Run in-process product experience replay fixtures.

Examples:
    python scripts/replay_experience.py
    python scripts/replay_experience.py --fixture tests/benchmark/fixtures/core.jsonl
    python scripts/replay_experience.py --memory-report ../eidolon_memory/reports/latest.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eidolon_agent.app.benchmark import (
    render_replay_html,
    render_replay_markdown,
    run_replay_files,
    run_replay_scenarios,
)
from eidolon_agent.app.benchmark.suites import (
    AGENT_MEMORY_BENCHMARK_NAME,
    agent_memory_experience_scenarios,
)


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
    parser.add_argument(
        "--agent-memory-benchmark",
        action="store_true",
        help=(
            "Run the built-in fast benchmark for agent + memory experience "
            f"({AGENT_MEMORY_BENCHMARK_NAME})."
        ),
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="Optional readable Markdown report path.",
    )
    parser.add_argument(
        "--html",
        type=Path,
        default=None,
        help="Optional self-contained HTML report path.",
    )
    args = parser.parse_args()

    if args.agent_memory_benchmark:
        report = await run_replay_scenarios(
            agent_memory_experience_scenarios(),
            memory_report_path=args.memory_report,
        )
    else:
        fixtures = args.fixture or [Path("tests/benchmark/fixtures/core_experience.jsonl")]
        report = await run_replay_files(fixtures, memory_report_path=args.memory_report)
    text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote replay report to {args.output}")
    else:
        print(text)
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_replay_markdown(report), encoding="utf-8")
        print(f"wrote readable report to {args.markdown}")
    if args.html is not None:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(render_replay_html(report), encoding="utf-8")
        print(f"wrote HTML report to {args.html}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
