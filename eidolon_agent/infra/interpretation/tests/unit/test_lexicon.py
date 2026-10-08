from __future__ import annotations

import pytest

from eidolon_agent.infra.interpretation.adapters.lexicon import (
    Values,
    Verb,
    command_for,
    normalize,
    parse_number,
    read_values,
    take,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("26", 26),
        ("25.5", 25.5),
        ("十", 10),
        ("十八", 18),
        ("二十六", 26),
        ("四十", 40),
        ("两", 2),
        ("一百", 100),
        ("一百零五", 105),
        ("二十五点五", 25.5),
        ("一半", 50),
        ("二六", None),
    ],
)
def test_parse_number(token, value) -> None:
    assert parse_number(token) == value


def test_normalize_folds_width_case_and_punctuation() -> None:
    assert normalize("  打开 客厅空调，调到２６℃！") == "打开客厅空调调到26°c"
    assert normalize("调到25.5度.") == "调到25.5度"
    assert normalize("ＯＫ，Ｇｏ") == "okgo"


def test_take_is_longest_first_and_blanks() -> None:
    text, hits = take("打开床头灯和灯", {"灯": "word", "床头灯": "name"})

    assert [(h.text, h.value, h.start) for h in hits] == [("床头灯", "name", 2), ("灯", "word", 6)]
    assert text == "打开###和#"


@pytest.mark.parametrize(
    ("text", "quantity", "problem"),
    [
        ("调到26度", (26, "celsius", "26度"), None),
        ("百分之三十", (30, "percent", "百分之三十"), None),
        ("亮度调到40", (40, None, "亮度调到40"), None),
        ("温度调到26", (26, "celsius", "温度调到26"), None),
        ("开一半", (50, "percent", "一半"), None),
        ("调高一点", None, None),
        ("调到26度再调到50%", None, "multiple_values"),
        ("打开2号", None, "multiple_values"),
    ],
)
def test_read_values(text, quantity, problem) -> None:
    _rest, values = read_values(text)

    got = values.quantity
    assert (None if got is None else (got.value, got.unit, got.raw_span)) == quantity
    assert values.problem == problem


def test_a_setting_without_a_v1_trait_is_not_read_as_brightness() -> None:
    _rest, values = read_values("色温调到4000")

    assert values.quantity is not None
    assert command_for("light", Verb("set"), values) is None


def test_modes_are_read_and_bound_to_thermostats() -> None:
    _rest, values = read_values("调成制热模式")
    assert values.mode == "heat"

    action = command_for("climate", Verb("set"), values)
    assert (action.trait, action.command, action.slots[0].value) == (
        "thermostat",
        "set_mode",
        "heat",
    )
    assert command_for("water_heater", Verb("set"), Values(mode="cool")) is None
    assert command_for("light", Verb("set"), values) is None


@pytest.mark.parametrize(
    ("kind", "op", "expected"),
    [
        ("light", "on", ("on_off", "on")),
        ("cover", "on", ("position", "open")),
        ("appliance", "on", ("operational", "start")),
        ("lock", "on", None),
        ("cover", "stop", ("position", "stop")),
        ("climate", "stop", ("on_off", "off")),
        ("media", "mute", ("volume", "mute")),
        ("light", "mute", None),
        ("climate", "up", ("thermostat", "step")),
        ("fan", "up", ("fan_speed", "step")),
        ("sensor", "off", None),
        ("scene", "on", ("scene", "activate")),
        ("scene", "off", None),
        ("unknown-kind", "on", None),
    ],
)
def test_verbs_bind_per_device_type(kind, op, expected) -> None:
    action = command_for(kind, Verb(op), Values())

    assert (None if action is None else (action.trait, action.command)) == expected
