"""Evaluate exported public DecisionRequests one at a time; never grants speech permits.

Input JSONL: {id, request: SDK DecisionRequest, gold: {action,speaker?,clarify_about?},
primary: SDK DecisionResult}. Gold is used only after model inference. Export model-specific
replays in their owning project; this runner has no Models runtime dependency.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import statistics
import time

from dotenv import load_dotenv
from eidolon_sdk.biz.participation import DecisionRequest, DecisionResult, validate_proposal

from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config.settings import load_settings
from eidolon_agent.infra.participation.llm import CLARIFY_INSTRUCTIONS, LlmParticipationDecision


class Measured:
    def __init__(self, llm):
        self.llm = llm
        self.usage = None

    @property
    def model_id(self):
        return self.llm.model_id

    async def stream(self, *args, **kwargs):
        from contextlib import aclosing

        async with aclosing(self.llm.stream(*args, **kwargs)) as stream:
            async for delta in stream:
                if delta.usage is not None:
                    self.usage = asdict(delta.usage)
                yield delta


def score(gold, result):
    p = result.proposal if result else None
    action = p.action if p else None
    action_ok = action == gold["action"]
    speaker_ok = (
        not p
        or action not in {"respond", "clarify"}
        or (len(p.participants) == 1 and p.participants[0] in gold.get("speaker", []))
    )
    reason_ok = (
        not p
        or action != "clarify"
        or p.instruction == CLARIFY_INSTRUCTIONS.get(gold.get("clarify_about"))
    )
    return {
        "action_correct": action_ok,
        "e2e_correct": bool(action_ok and speaker_ok and reason_ok),
        "unwanted_speech": gold["action"] in {"wait", "finish"}
        and action in {"respond", "clarify"},
    }


async def run(args):
    raw = args.cases.read_bytes()
    cases = [json.loads(line) for line in raw.splitlines() if line.strip()]
    # Validate the complete input before making any paid model requests.
    validated = []
    seen = set()
    for case in cases:
        req = DecisionRequest.model_validate(case["request"])
        primary = DecisionResult.model_validate(case["primary"])
        validate_proposal(req, primary)
        if case["id"] != req.decision_id or case["id"] in seen:
            raise ValueError("invalid or duplicate case id")
        seen.add(case["id"])
        if case["gold"]["action"] not in {"respond", "clarify", "wait", "finish"}:
            raise ValueError("invalid gold")
        validated.append((case, req, primary))
    args.output.mkdir(parents=True, exist_ok=False)
    for path in args.env_file:
        load_dotenv(path)
    llm = _build_llm_router(load_settings(yaml_path=args.settings))
    if llm.model_id == "fake":
        raise ValueError("evaluation requires the configured product model")
    measured = Measured(llm)
    port = LlmParticipationDecision(measured, timeout_ms=args.timeout_ms)
    rows = []
    try:
        with (args.output / "results.jsonl").open("w") as f:
            for case, req, primary in validated:
                if args.abstained_only and primary.status != "abstained":
                    continue
                measured.usage = None
                start = time.perf_counter()
                result = None
                error = None
                try:
                    result = await port(req.model_copy(update={"timeout_ms": args.timeout_ms}))
                except Exception as exc:
                    error = getattr(exc, "code", type(exc).__name__)
                row = {
                    "id": case["id"],
                    "primary_status": primary.status,
                    "gold": case["gold"],
                    "result": result.model_dump(mode="json") if result else None,
                    "error": error,
                    "ms": round((time.perf_counter() - start) * 1000, 1),
                    "usage": measured.usage,
                    **score(case["gold"], result),
                }
                rows.append(row)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                print(case["id"], row["e2e_correct"], row["ms"], error, flush=True)
    finally:
        await llm.close()
    values = sorted(r["ms"] for r in rows)
    summary = {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "configured_model": llm.model_id,
        "timeout_ms": args.timeout_ms,
        "n": len(rows),
        "action_correct": sum(r["action_correct"] for r in rows),
        "e2e_correct": sum(r["e2e_correct"] for r in rows),
        "unwanted_speech": sum(r["unwanted_speech"] for r in rows),
        "errors": sum(r["error"] is not None for r in rows),
        "p50_ms": statistics.median(values) if values else None,
        "p95_ms": values[math.ceil(0.95 * len(values)) - 1] if values else None,
        "max_ms": max(values) if values else None,
    }
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-ms", type=int, default=3000)
    parser.add_argument("--abstained-only", action="store_true")
    asyncio.run(run(parser.parse_args()))
