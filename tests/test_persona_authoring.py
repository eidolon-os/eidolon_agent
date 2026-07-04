"""Unit tests for companion-first genome authoring (pure functions)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eidolon_agent.domain.personas.authoring import assemble_genome, render_authored_markdown


def test_assemble_genome_builds_valid_persona() -> None:
    persona = assemble_genome(
        owner_id="o1",
        companion_id="c1",
        name="小马",
        values=["温柔", "可靠"],
        taboos=["说谎"],
        goals=["帮主人整理记忆"],
        pinned_facts=["主人叫 Manson"],
        style=["用短句"],
        knobs={"warmth": 0.8},
    )
    assert persona.owner_id == "o1"
    assert persona.companion_id == "c1"
    assert persona.version == 1
    assert persona.metadata.name == "小马"
    assert persona.metadata.template_id == "authored"
    assert persona.identity_core.values == ("温柔", "可靠")
    assert persona.identity_core.taboos == ("说谎",)
    assert persona.goals == ("帮主人整理记忆",)
    assert persona.pinned_facts == ("主人叫 Manson",)
    assert persona.style_compiler.base_instructions == ("用短句",)
    assert persona.behavioral_knobs["warmth"].current == pytest.approx(0.8)


def test_assemble_genome_rejects_out_of_range_knob() -> None:
    with pytest.raises(ValidationError):
        assemble_genome(owner_id="o", companion_id="c", name="x", knobs={"bad": 2.0})


def test_render_authored_markdown_includes_sections() -> None:
    persona = assemble_genome(
        owner_id="o", companion_id="c", name="小马", values=["温柔"], goals=["陪伴"]
    )
    md = render_authored_markdown(persona)
    assert "# 小马" in md
    assert "价值观" in md and "温柔" in md
    assert "目标" in md and "陪伴" in md
