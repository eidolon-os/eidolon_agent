"""Shared test helpers (non-fixture utilities).

Fixtures live in the project-root ``conftest.py`` (auto-discovered by
pytest from every test directory). Plain helper functions like
``make_turn_input`` are imported explicitly via this module.
"""

from __future__ import annotations

from eidolon_sdk.biz.persona import (
    PERSONA_GENOME_SCHEMA,
    PERSONA_REALIZER,
    build_default_persona_genome,
    persona_genome_hash,
)

from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.turn import TurnInput, TurnTrigger


def make_turn_input(text: str = "你好") -> TurnInput:
    genome_hash = persona_genome_hash(build_default_persona_genome(name="Test Companion"))
    return TurnInput(
        turn_id="t1",
        conversation_id="c1",
        session_id="s1",
        caller=CallerContext(
            identity=Identity(
                owner_id="alice",
                companion_id="companion-test",
                device_id="device-test",
                memory_realm_id="realm-test",
                genome_id="genome-test",
                schema_version=PERSONA_GENOME_SCHEMA,
                genome_hash=genome_hash,
                realizer_version=PERSONA_REALIZER,
            ),
            caller_kind=CallerKind.WEB_CHAT,
            trace_id="tr",
            request_id="rq",
            runtime_caller_id="rc-test",
            runtime_session_id="s1",
            actor_kind="web_chat",
            actor_id="device-test",
            display_name="Test Caller",
            transport="test",
        ),
        trigger=TurnTrigger.USER_UTTERANCE,
        text=text,
    )
