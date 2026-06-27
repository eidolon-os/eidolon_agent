"""Replay helpers for conversations and rollout gates.

    python scripts/replay_conversation.py --conv conv-sim
    python scripts/replay_conversation.py --conv conv-sim --export-traces /tmp/base.json
    python scripts/replay_conversation.py --diff-baseline /tmp/base.json --diff-candidate /tmp/new.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eidolon_data import DataStore
from eidolon_data import load_settings as load_data_settings
from eidolon_data.schema.models import MessageRow, TurnRow
from sqlalchemy import select

from eidolon_agent.infra.observability.replay_diff import (
    ReplayDiffThresholds,
    compare_replay_snapshots,
    snapshots_from_artifact,
)


async def _print_conversation(conv_id: str) -> None:
    settings = load_data_settings()
    store = DataStore.open(settings)
    async with store.session_factory() as s:
        rows = (
            await s.execute(
                select(MessageRow, TurnRow)
                .join(TurnRow, MessageRow.turn_id == TurnRow.turn_id)
                .where(TurnRow.conversation_id == conv_id)
                .order_by(TurnRow.seq, MessageRow.created_at)
            )
        ).all()
    if not rows:
        print(f"no messages for {conv_id} in {settings.sqlite_path}")
        await store.close()
        return
    for msg, _turn in rows:
        ts = msg.created_at.isoformat(timespec="seconds")
        print(f"[{ts}] {msg.role:>9}: {msg.content}")
    await store.close()


async def _export_traces(conv_id: str, path: Path) -> None:
    store = DataStore.open(load_data_settings())
    async with store.session_factory() as s:
        turns = (
            (
                await s.execute(
                    select(TurnRow)
                    .where(TurnRow.conversation_id == conv_id)
                    .order_by(TurnRow.seq, TurnRow.started_at)
                )
            )
            .scalars()
            .all()
        )
    artifact = {
        "schema_version": "eidolon_agent.replay_trace_artifact.v1",
        "conversation_id": conv_id,
        "turns": [
            {
                "turn_id": row.turn_id,
                "metadata": {**(row.metadata_json or {}), "turn_trace": row.trace_json},
            }
            for row in turns
            if row.trace_json
        ],
    }
    path.write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    print(f"exported {len(artifact['turns'])} traced turns to {path}")
    await store.close()


def _run_diff(args: argparse.Namespace) -> int:
    baseline = snapshots_from_artifact(_read_json(args.diff_baseline))
    candidate = snapshots_from_artifact(_read_json(args.diff_candidate))
    report = compare_replay_snapshots(
        baseline,
        candidate,
        thresholds=ReplayDiffThresholds(
            max_prompt_change_ratio=args.max_prompt_change_ratio,
            max_tool_decision_change_ratio=args.max_tool_decision_change_ratio,
            max_latency_regression_ratio=args.max_latency_regression_ratio,
            max_latency_regression_ms=args.max_latency_regression_ms,
        ),
    )
    print(json.dumps(report.to_metadata(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report.passed else 1


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


async def main(args: argparse.Namespace) -> int:
    if args.diff_baseline or args.diff_candidate:
        if not args.diff_baseline or not args.diff_candidate:
            raise SystemExit("--diff-baseline and --diff-candidate must be provided together")
        return _run_diff(args)
    if not args.conv:
        raise SystemExit("--conv is required unless running --diff-baseline/--diff-candidate")
    if args.export_traces:
        await _export_traces(args.conv, args.export_traces)
    else:
        await _print_conversation(args.conv)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--conv")
    ap.add_argument("--export-traces", type=Path)
    ap.add_argument("--diff-baseline", type=Path)
    ap.add_argument("--diff-candidate", type=Path)
    ap.add_argument("--max-prompt-change-ratio", type=float, default=0.05)
    ap.add_argument("--max-tool-decision-change-ratio", type=float, default=0.0)
    ap.add_argument("--max-latency-regression-ratio", type=float, default=0.25)
    ap.add_argument("--max-latency-regression-ms", type=int, default=150)
    sys.exit(asyncio.run(main(ap.parse_args())))
