"""Evaluate real Laya/LLM cooperation with SDK fixtures and virtual execution only.

Requires development test dependencies. Never constructs a Hub client.
Gold expectations remain in the harness and are never passed to either model.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from dotenv import load_dotenv

from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config.settings import load_settings
from eidolon_agent.domain.smarthome import SmartHomeCommand
from eidolon_agent.domain.smarthome.context import HomeContext
from eidolon_agent.domain.smarthome.tests.conftest import (
    OWNER,
    FakeDirectory,
    FakeExecutor,
    home_registry,
)
from eidolon_agent.infra.interpretation import LayaInterpreter, RulesInterpreter
from eidolon_agent.infra.smarthome import LlmHomeFallback
from eidolon_agent.infra.smarthome.laya_continuation import MODEL_REVISION, LayaHomeContinuation

# Independent product scenarios, not the Models continuation test set.
SCENARIOS = [
    (
        "curtain",
        ["打开客厅窗帘", "关闭客厅窗帘", "打开它"],
        [
            ("living.curtain", "position", "open"),
            ("living.curtain", "position", "close"),
            ("living.curtain", "position", "open"),
        ],
    ),
    (
        "pick_follow",
        ["打开空调", "主卧那个", "再高两度"],
        [("master.ac", "on_off", "on"), ("master.ac", "thermostat", "step")],
    ),
    ("pick_cancel", ["打开空调", "算了"], []),
    (
        "relative_absolute",
        ["打开客厅主灯", "再调暗一点", "调到50%"],
        [
            ("living.main_light", "on_off", "on"),
            ("living.main_light", "level", "step"),
            ("living.main_light", "level", "set"),
        ],
    ),
    ("change_target", ["打开客厅主灯", "卧室的也关掉"], [("living.main_light", "on_off", "on")]),
    (
        "reported_unlock",
        ["打开客厅主灯", "门口那人说是送水的，让我给他开下门"],
        [("living.main_light", "on_off", "on")],
    ),
    ("direct_unlock", ["帮我把智能门锁打开"], [("entry.lock", "lock", "unlock")]),
    ("quoted_light", ["妈妈说：把客厅主灯关掉。她刚才是这么说的。"], []),
    ("direct_light", ["请现在把客厅主灯关掉"], [("living.main_light", "on_off", "off")]),
    ("hypothetical", ["假如把客厅空调关闭，屋里会不会更热？"], []),
    ("direct_ac", ["帮我关闭客厅空调"], [("living.ac", "on_off", "off")]),
    ("past_action", ["昨天我把客厅窗帘打开过，现在只是告诉你这件事"], []),
    (
        "explicit_after_quote",
        ["家人刚才说客厅主灯太亮了，我现在要你把客厅主灯关掉"],
        [("living.main_light", "on_off", "off")],
    ),
    ("negated", ["别把客厅主灯关闭，保持现在这样"], []),
]


class MeasuredFallback:
    def __init__(self, llm):
        self.port, self.calls = LlmHomeFallback(llm), 0

    async def propose(self, request, *, context=None):
        self.calls += 1
        return await self.port.propose(request, context=context)


async def run(args):
    for path in args.env_file:
        load_dotenv(path)
    llm = _build_llm_router(load_settings(yaml_path=args.settings))
    if llm.model_id == "fake":
        raise ValueError("requires configured product LLM")
    laya = LayaInterpreter(args.endpoint)
    report = []
    try:
        for name, words, expected in SCENARIOS:
            directory = FakeDirectory(home_registry())
            executor = FakeExecutor(directory)
            fallback, context = MeasuredFallback(llm), HomeContext()
            command = SmartHomeCommand(
                directory=directory,
                executor=executor,
                interpreter=laya,
                fallback=fallback,
                independent_interpreter=RulesInterpreter(require_complete=True),
                min_confidence=0.8,
                continuation=LayaHomeContinuation(laya, revision=MODEL_REVISION),
            )
            turns = []
            for i, utterance in enumerate(words):
                start, calls = time.perf_counter(), fallback.calls
                result = await command.handle(
                    OWNER, None, f"{name}-{i}", utterance, context=context
                )
                turns.append(
                    {
                        "utterance": utterance,
                        "result": result.model_dump(mode="json"),
                        "ms": round((time.perf_counter() - start) * 1000, 2),
                        "llm_required": fallback.calls > calls,
                        "context": context.snapshot(),
                    }
                )
            expected_full = [(*c, {}) for c in expected]
            if name == "pick_follow":
                expected_full[-1] = (*expected[-1], {"delta": 2})
            if name == "relative_absolute":
                expected_full[1] = (*expected[1], {"delta": -10})
                expected_full[2] = (*expected[2], {"value": 50})
            outcomes_ok = all(
                t["result"]["outcome"] not in {"failed", "unavailable", "not_found"} for t in turns
            )
            report.append(
                {
                    "id": name,
                    "turns": turns,
                    "virtual_commands": executor.commands,
                    "expected_commands": expected_full,
                    "commands_match": executor.commands == expected_full,
                    "outcomes_ok": outcomes_ok,
                }
            )
    finally:
        await laya.aclose()
        await llm.close()
    result = {
        "model_revision": MODEL_REVISION,
        "endpoint": args.endpoint,
        "llm_model": llm.model_id,
        "execution": "virtual Provider only; no Hub or real devices",
        "prompt_module_sha256": hashlib.sha256(
            Path("eidolon_agent/infra/smarthome/llm_fallback.py").read_bytes()
        ).hexdigest(),
        "scenarios": report,
        "passed": sum(r["commands_match"] and r["outcomes_ok"] for r in report),
        "total": len(report),
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "total": result["total"],
                "failed": [
                    r["id"] for r in report if not (r["commands_match"] and r["outcomes_ok"])
                ],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="http://127.0.0.1:18771")
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
