"""Text → Agent home command → Hub → Provider, against a running Hub, without a microphone.

The same domain code the voice path runs (``SmartHomeCommand`` with the rules
interpreter and the real ``HubSmartHomeClient``), minus ASR, Companion
authorisation and the LLM fallback. What it proves: the Agent's understanding
and the Hub's execution agree end to end on real Provider devices (the Home
Assistant bench, or 周边好生活's cloud mock). What it does not prove: speech
recognition, the Companion binding, LLM understanding.

    uv run python scripts/bench_home_hub_e2e.py --owner <owner_id> \
        --env ../.eidolon/mac-product/config/env/hub.env --device-ref korvo-bench \
        "打开厨房灯" "关闭厨房灯" "打开大厅窗帘"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from eidolon_agent.domain.smarthome.command import SmartHomeCommand
from eidolon_agent.domain.smarthome.context import HomeContext
from eidolon_agent.infra.interpretation.adapters.rules import RulesInterpreter
from eidolon_agent.infra.smarthome.hub import HubSmartHomeClient


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hub", default="http://127.0.0.1:8082")
    parser.add_argument("--env", type=Path, default=Path("../.eidolon/mac-product/config/env/hub.env"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--device-ref", default=None, help="the speaking panel, for its room as the default area")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("utterances", nargs="+")
    args = parser.parse_args()
    token = read_env(args.env)["EIDOLON_HUB_SMARTHOME_TOKEN"]
    client = HubSmartHomeClient(base_url=args.hub, token=token)
    command = SmartHomeCommand(
        directory=client,
        executor=client,
        interpreter=RulesInterpreter(),
        fallback=None,
        min_confidence=0.0,
        continuation=None,
        independent_interpreter=RulesInterpreter(require_complete=True),
    )
    context = HomeContext()
    rows = []
    try:
        for index, utterance in enumerate(args.utterances):
            started = time.monotonic()
            result = await command.handle(args.owner, args.device_ref, f"bench-{index}", utterance, context=context)
            elapsed = (time.monotonic() - started) * 1000
            row = {"utterance": utterance, "outcome": result.outcome, "message": result.message, "elapsed_ms": round(elapsed, 1)}
            rows.append(row)
            print(f"{utterance:<16} {result.outcome:<13} {elapsed:7.1f} ms  {result.message}")
    finally:
        await client.aclose()
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0 if all(r["outcome"] in ("executed", "answered") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
