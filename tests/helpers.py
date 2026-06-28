"""Shared test helpers (non-fixture utilities).

Fixtures live in the project-root ``conftest.py`` (auto-discovered by
pytest from every test directory). Plain helper functions like
``make_turn_input`` are imported explicitly via this module.
"""

from __future__ import annotations

from eidolon_agent.core.types.identity import CallerContext, CallerKind, Identity
from eidolon_agent.core.types.turn import TurnInput, TurnTrigger


def make_turn_input(text: str = "你好") -> TurnInput:
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
            ),
            caller_kind=CallerKind.WEB_CHAT,
            trace_id="tr",
            request_id="rq",
        ),
        trigger=TurnTrigger.USER_UTTERANCE,
        text=text,
    )
