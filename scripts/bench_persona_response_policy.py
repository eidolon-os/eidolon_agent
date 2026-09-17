"""Synthetic response-policy A/B through the production Compiler and LLM router.

No user database, tools, microphone or memory writes. --execute explicitly calls
the configured model; without it this only writes the compiled test manifest.
Supply --preset-catalog with a Data persona-presets JSON response or package export.
This measures model text, not end-to-end voice latency or playback continuity.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from dataclasses import replace
from pathlib import Path

from eidolon_sdk.biz.persona import (
    PERSONA_REALIZER,
    ConversationPreferences,
    PersonaAuthoringDraft,
    PersonaPresetCatalog,
    build_persona_genome_from_draft,
    persona_genome_hash,
)

from eidolon_agent.config import load_settings
from eidolon_agent.core.types.turn import TurnInput, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.context.response_policy import (
    RESPONSE_POLICY_VERSION,
    response_policy_prompt,
)
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.domain.personas.realizer import PersonaRealizer
from eidolon_agent.domain.personas.types import StoredPersonaGenome

SCENARIOS = [
    ("greeting", "你好。"),
    ("emotion", "今天有点累。"),
    ("fact", "一年有几个月？"),
    ("detail", "请详细解释缓存和内存的区别，给三个具体例子。"),
    ("ack", "好的，我知道了。"),
    ("thanks", "谢谢你。"),
    ("emotion", "今天被领导批评了，很委屈。"),
    ("emotion", "我只是想说说，不需要建议。"),
    ("advice", "明天面试，有什么准备建议？"),
    ("advice", "给我三条保持专注的建议。"),
    ("clarification", "帮我做个选择。"),
    ("clarification", "帮我把它改一下。"),
    ("fact", "水在标准大气压下几度沸腾？"),
    ("fact", "17 加 26 是多少？"),
    ("brief", "一句话解释什么是递归。"),
    ("brief", "请只回答是或否：10 大于 3 吗？"),
    ("detail", "比较三种记笔记方法，说明优缺点和适用场景。"),
    ("detail", "给我一个完整的两周英语口语练习计划。"),
    ("task", "帮我写一条礼貌拒绝今晚聚餐的短信。"),
    ("task", "把‘明天再联系’翻译成英文，只给译文。"),
    ("memory", "你记得我昨天吃了什么吗？"),
    ("memory", "我之前跟你提过哪些旅行计划？"),
    ("boundary", "替我承诺下周一定帮朋友搬家。"),
    ("failure", "你查不到天气就直接告诉我，不要猜。"),
    ("stop", "先停一下，不用继续了。"),
    ("correction", "刚才太长了，说短一点。"),
    ("chat", "今天终于把桌面收拾好了。"),
    ("chat", "窗外下雨了。"),
    ("chat", "刚喝到一杯很好喝的茶。"),
    ("chat", "我想安静待一会儿。"),
    ("chat", "明天周末啦。"),
    ("chat", "今天暂时没有别的事。"),
]


class DraftPersona:
    def __init__(self, stored):
        self.stored = stored

    async def realize_context(self, **kwargs):
        return PersonaRealizer().realize(
            stored=self.stored, modality=kwargs.get("modality", "text")
        )


def load_benchmark_preset(path: Path, preset_id: str):
    """Read an explicit Data catalogue snapshot; no SDK or local fallback."""
    catalog = PersonaPresetCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    matches = [preset for preset in catalog.presets if preset.preset_id == preset_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one preset {preset_id!r} in {path}")
    return matches[0]


async def run(args):
    preset = load_benchmark_preset(args.preset_catalog, args.preset)
    settings = load_settings()
    router = None
    if args.execute:
        from eidolon_agent.app.runtime.bootstrap import _build_llm_router

        router = _build_llm_router(settings)
    genome = build_persona_genome_from_draft(
        PersonaAuthoringDraft.for_companion(preset.persona, name="测试伙伴")
    )
    stored = StoredPersonaGenome(
        owner_id="synthetic-owner",
        companion_id="synthetic-companion",
        genome_id="synthetic-genome",
        genome_hash=persona_genome_hash(genome),
        realizer_version=PERSONA_REALIZER,
        version=1,
        genome=genome,
    )
    compiler = ContextCompiler(
        personas_service=DraftPersona(stored),
        instance_locator=lambda *_: (stored.companion_id, stored.genome_id),
        history_manager=HistoryManager(),
    )
    rows = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.output.open("w") as output:
            for index, (category, text) in enumerate(SCENARIOS[: args.limit]):
                turn = TurnInput(
                    turn_id=f"eval-{index}",
                    conversation_id=f"eval-{index}",
                    session_id="eval",
                    context=TurnContext(
                        owner_id=stored.owner_id,
                        companion_id=stored.companion_id,
                        device_id=None,
                        memory_realm_id="synthetic-realm",
                        genome_id=stored.genome_id,
                        trace_id=f"eval-{index}",
                        request_id=f"eval-{index}",
                        schema_version=genome.schema_version,
                        genome_hash=stored.genome_hash,
                        realizer_version=PERSONA_REALIZER,
                    ),
                    input_modality=args.modality,
                    trigger=TurnTrigger.USER_UTTERANCE,
                    text=text,
                )
                compiled = await compiler.compile(turn)
                policy = response_policy_prompt(ConversationPreferences(), modality=args.modality)
                # Alternate ordering; provider cache state is observed, never claimed cold/hot.
                for enabled in [False, True] if index % 2 == 0 else [True, False]:
                    messages = (
                        compiled
                        if enabled
                        else [replace(m, content=m.content.replace(policy, "")) for m in compiled]
                    )
                    row = dict(
                        case=index,
                        category=category,
                        prompt=text,
                        policy=RESPONSE_POLICY_VERSION if enabled else "baseline_without_policy",
                        preset=preset.preset_id,
                        preset_revision=preset.revision,
                        genome_hash=stored.genome_hash,
                        model=settings.llm.default_model,
                        modality=args.modality,
                        memory_state="not_connected",
                        cache_state="uncontrolled",
                        executed=args.execute,
                        prompt_digest=hashlib.sha256(
                            "\n".join(m.content for m in messages).encode()
                        ).hexdigest(),
                    )
                    if router:
                        started = time.monotonic()
                        answer = []
                        row["first_content_ms"] = None
                        try:
                            async with asyncio.timeout(args.timeout):
                                async for delta in router.stream(
                                    messages, temperature=0.0, request_id=f"eval-{index}-{enabled}"
                                ):
                                    if delta.text_delta:
                                        if row["first_content_ms"] is None:
                                            row["first_content_ms"] = round(
                                                (time.monotonic() - started) * 1000
                                            )
                                        answer.append(delta.text_delta)
                                    if delta.finish:
                                        row["finish"] = delta.finish.value
                        except Exception as exc:
                            row["error_type"] = type(exc).__name__
                        row.update(
                            answer="".join(answer),
                            total_ms=round((time.monotonic() - started) * 1000),
                        )
                        row["characters"] = len(row["answer"])
                        row["sentences"] = len(
                            [s for s in re.split(r"[。！？!?]+", row["answer"]) if s.strip()]
                        )
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    output.flush()
                    rows.append(row)
                    print(
                        f"case={index} policy={enabled} executed={args.execute} error={row.get('error_type', '')}",
                        flush=True,
                    )
    finally:
        if router:
            await router.close()
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, default=32)
    parser.add_argument(
        "--preset-catalog",
        type=Path,
        required=True,
        help="JSON response from Data's persona-presets endpoint (or a Data package export)",
    )
    parser.add_argument("--preset", default="gentle", help="Preset id in the supplied catalogue")
    parser.add_argument("--modality", choices=["voice", "text"], default="voice")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
