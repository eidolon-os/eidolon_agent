"""Synthetic public-only role evaluation using the configured shared LLM.

Run with the deployment's usual environment and optional --config. Does not
start services, access Companion/private data, open a team, or operate devices.
Output is evidence for human review, not an automatic semantic pass assertion.
"""

import argparse
import asyncio
import json
from pathlib import Path
from time import monotonic

from eidolon_sdk.biz.control.coordination import SceneRole
from eidolon_sdk.biz.participation import Context, Message
from eidolon_sdk.biz.persona import (
    PERSONA_GENOME_SCHEMA,
    PERSONA_REALIZER,
    build_default_persona_genome,
    persona_genome_hash,
)

from eidolon_agent.app.interaction.coordination.role_reply import (
    RoleMember,
    RoleReplyExecutor,
    RoleReplyRequest,
)
from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config import load_settings
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeConfig, CompanionRuntimeFacts
from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.domain.runtime_session import AuthorizedRuntimeSession


async def evaluate(config):
    settings = load_settings(yaml_path=config)
    llm = _build_llm_router(settings)
    if llm.model_id == "fake":
        raise RuntimeError("real configured model required")
    executor = RoleReplyExecutor(llm)
    roles = (RoleMember("a", SceneRole(name="孙悟空")), RoleMember("b", SceneRole(name="猪八戒")))
    scopes = {}
    for member in roles:
        genome = build_default_persona_genome(name="测试伙伴" + member.companion_id)
        facts = CompanionRuntimeFacts(
            "role-eval",
            member.companion_id,
            "eval-unused",
            "eval-genome",
            1,
            PERSONA_GENOME_SCHEMA,
            persona_genome_hash(genome),
            PERSONA_REALIZER,
            genome,
            {},
        )
        scopes[member.companion_id] = AuthorizedRuntimeSession(
            facts, "eval-input", "role-eval-scene", CompanionRuntimeConfig()
        )
    cases = [
        ("fruit", "你们讨论一下最喜欢的水果。", False),
        ("whole_script", "孙悟空和猪八戒来回聊三轮，给我一整段有两个人台词的对话。", False),
        ("after_window", "接着说说周末去哪里玩，回应上一位的建议。", True),
    ]
    evidence = []
    for case, text, long_history in cases:
        history = []
        if long_history:
            for i in range(20):
                history.append(
                    Message(
                        message_id=f"old-{i}",
                        author_kind="companion",
                        author_id="a" if i % 2 == 0 else "b",
                        text="周末可以去山里看风景。" if i % 2 == 0 else "路上要记得安排吃饭休息。",
                    )
                )
        trigger = Message(message_id=case, author_kind="user", author_id="eval-input", text=text)
        history.append(trigger)
        # A -> B then a genuine peer-triggered A continuation, not another ASR.
        for index, member in enumerate((roles[0], roles[1], roles[0])):
            source = history[-1] if index == 2 else trigger
            request = RoleReplyRequest(
                scopes[member.companion_id],
                "role-eval-scene",
                f"eval-{case}-{index}",
                1,
                roles,
                source,
                Context(recent_messages=tuple(history[-16:])),
            )
            started = monotonic()
            response = ""
            async for event in executor.run(request):
                if event.kind is TurnEventKind.DELTA:
                    response += event.data["text"]
            history.append(
                Message(
                    message_id=request.turn_id,
                    author_kind="companion",
                    author_id=member.companion_id,
                    text=response,
                )
            )
            item = dict(
                case=case,
                role=member.role.name,
                source=source.author_kind,
                seconds=round(monotonic() - started, 3),
                text=response,
            )
            evidence.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
    return dict(model=llm.model_id, synthetic=True, semantic_review="required", replies=evidence)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(evaluate(args.config))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
