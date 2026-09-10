"""Real-model, multi-turn user journeys against isolated production HTTP apps.

Run from Agent with PYTHONPATH=.:../eidolon_admin/server and --execute-model.
Requires the sibling Admin checkout and psutil in the test environment.
Only synthetic test identities/SQLite are used; no Memory or tool writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
import time
from pathlib import Path

from pytest import MonkeyPatch

from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config import load_settings
from tests.e2e.persona_conversation import Conversation, MemoryCondition
from tests.e2e.persona_network_stack import persona_stack
from tests.e2e.test_persona_network_journeys import create, request

SCENARIOS = [
    ("greeting", "你好，我们随便聊几句。", "empty"),
    ("context", "我养了一只猫，叫{name}。这次聊天记得就好。", "empty"),
    ("emotion", "今天工作有点累，我只是想说说，不需要建议。", "unavailable"),
    ("brief", "用一句话告诉我，你听到了什么。", "timeout"),
    ("advice", "明天要面试，请给我三条具体准备建议。", "empty"),
    ("detail", "请详细解释怎么准备自我介绍，分三个步骤，每步给一个例子。", "empty"),
    ("memory", "你记得我喜欢喝什么茶吗？", "kg_only"),
    ("task", "帮我写一条婉拒今晚聚餐的短信，只给短信正文。", "empty"),
    ("recall", "刚才我说我的猫叫什么？只回答名字。", "empty"),
    ("stop", "好，我想安静待一会儿。", "empty"),
]


async def main(args):
    if not args.execute_model:
        raise SystemExit(
            "Pass --execute-model to call the configured provider with synthetic journeys."
        )
    rows = []
    scenarios = list(SCENARIOS)
    if args.speech_input:
        speech = json.loads(Path(args.speech_input).read_text())["results"]
        by_case = {row["case"]: row["transcript"] for row in speech}
        scenarios[2] = ("emotion", by_case["speech-emotion"], "unavailable")
        scenarios[5] = ("detail", by_case["speech-detail"], "empty")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    router = _build_llm_router(load_settings())
    with (
        tempfile.TemporaryDirectory(prefix="persona-journey-") as directory,
        MonkeyPatch.context() as patch,
    ):
        async with persona_stack(Path(directory), patch) as stack:
            stack.model.delegate = router
            try:
                presets = (await request(stack, "GET", "/persona-presets"))["presets"]
                for preset, cat in zip(presets, ["芝麻", "汤圆", "花卷"], strict=True):
                    companion = await create(stack, name="小南", persona=preset["persona"])
                    facts = await stack.runtime.resolve(
                        owner_id="owner-journey", companion_id=companion
                    )
                    memory = MemoryCondition()
                    conversation = Conversation(stack, facts, memory=memory)
                    for index, (kind, template, condition) in enumerate(scenarios):
                        text = template.format(name=cat)
                        memory.state = condition
                        started = time.monotonic()
                        async with asyncio.timeout(45):
                            answer, events = await conversation.say(text)
                        last = events[-1]
                        row = {
                            "preset": preset["preset_id"],
                            "turn": index + 1,
                            "kind": kind,
                            "input": text,
                            "memory_condition": condition,
                            "reply": answer,
                            "input_source": "real_stt"
                            if args.speech_input and kind in {"emotion", "detail"}
                            else "text",
                            "elapsed_ms": round((time.monotonic() - started) * 1000),
                            "status": last.data.get("status"),
                            "done": last.data,
                            "chars": len(answer),
                            "question_marks": len(re.findall(r"[?？]", answer)),
                            "history_messages": len(
                                await conversation.history.recent_window(
                                    conversation_id=conversation.conversation_id
                                )
                            ),
                            "context_budget": conversation.last_turn.metadata.get(
                                "development_guards", {}
                            ).get("context_budget"),
                            "history_window": conversation.last_turn.metadata.get(
                                "history_window_applied"
                            ),
                            "checks": {
                                "completed": last.data.get("status") == "ok",
                                "not_empty": bool(answer.strip()),
                                "not_truncated": not conversation.last_turn.metadata.get(
                                    "output_truncated", False
                                ),
                                **(
                                    {
                                        "context_recalled": cat in answer,
                                        "no_other_companion_fact": all(
                                            n not in answer
                                            for n in ["芝麻", "汤圆", "花卷"]
                                            if n != cat
                                        ),
                                    }
                                    if kind == "recall"
                                    else {}
                                ),
                                **(
                                    {"explicit_brief": len(answer) <= 100}
                                    if kind == "brief"
                                    else {}
                                ),
                                **({"memory_fact": "茉莉" in answer} if kind == "memory" else {}),
                            },
                        }
                        rows.append(row)
                        output.write_text(
                            "".join(
                                json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows
                            )
                        )
                        print(
                            json.dumps(
                                {
                                    k: row[k]
                                    for k in ["preset", "turn", "kind", "chars", "status", "checks"]
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
            finally:
                await router.close()
    failed = [r for r in rows if not all(r["checks"].values())]
    print(
        f"{len(rows)} real-model turns; {len(failed)} rows require investigation; artifact={output}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-model", action="store_true")
    parser.add_argument("--output", default="docs/reviews/persona-multiturn-journeys.jsonl")
    parser.add_argument("--speech-input", help="input.json from validate_persona_audio.py")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
