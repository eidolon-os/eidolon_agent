"""PersonasService — version is bumped on every applied evolution.

Exercises the explicit-evolve path: starting at version 1, applying a rule
that matches the instance must bump the stored overlay to version 2 and
record the audit row.
"""

from __future__ import annotations

import pytest

from eidolon_agent.domain.personas.types import PersonaEvolutionEvent

pytestmark = pytest.mark.functional


async def test_version_increments_on_evolve(personas_service) -> None:
    await personas_service.create_instance(
        owner_id="u",
        companion_id="i-ver",
        template_id="caretaker_jiezhi",
    )
    before = await personas_service.get_instance(
        owner_id="u", companion_id="i-ver"
    )
    assert before.version == 1

    result = await personas_service.evolve(
        owner_id="u",
        companion_id="i-ver",
        events=[PersonaEvolutionEvent(kind="positive_feedback_received", source="test")],
    )
    assert result.applied is True

    after = await personas_service.get_instance(
        owner_id="u", companion_id="i-ver"
    )
    assert after.version == 2


async def test_dry_run_does_not_bump_version(personas_service) -> None:
    await personas_service.create_instance(
        owner_id="u",
        companion_id="i-dry",
        template_id="caretaker_jiezhi",
    )
    result = await personas_service.evolve(
        owner_id="u",
        companion_id="i-dry",
        events=[PersonaEvolutionEvent(kind="positive_feedback_received", source="test")],
        dry_run=True,
    )
    assert result.applied is False
    after = await personas_service.get_instance(
        owner_id="u", companion_id="i-dry"
    )
    assert after.version == 1
