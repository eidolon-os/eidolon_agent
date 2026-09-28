from __future__ import annotations

import pytest

from eidolon_agent.core.ports.tool import ToolInvocationContext, ToolPort
from eidolon_agent.core.types.tool import ToolCall
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.smarthome import smart_home_tools
from eidolon_agent.domain.smarthome.tests.conftest import OWNER

pytestmark = pytest.mark.unit


def _ctx(device_id: str | None = "panel-living") -> ToolInvocationContext:
    return ToolInvocationContext(
        turn_context=TurnContext(
            owner_id=OWNER,
            companion_id="companion-1",
            device_id=device_id,
            memory_realm_id="realm-1",
            genome_id="genome-1",
            trace_id="trace-1",
            request_id="req-1",
        ),
        input_modality="voice",
        turn_id="turn-1",
    )


@pytest.fixture
def tools(directory, executor) -> dict[str, ToolPort]:
    return {tool.schema.name: tool for tool in smart_home_tools(directory, executor)}


async def _call(tools, tool: str, ctx=None, call_id: str = "call-1", **arguments):
    call = ToolCall(id=call_id, name=tool, arguments=arguments)
    return await tools[tool].invoke(call, ctx=ctx or _ctx())


def test_six_fixed_tools(tools) -> None:
    assert sorted(tools) == [
        "home_activate_scene",
        "home_adjust",
        "home_get_state",
        "home_set",
        "home_turn_off",
        "home_turn_on",
    ]
    assert all(isinstance(tool, ToolPort) for tool in tools.values())
    assert tools["home_get_state"].schema.side_effect is False
    for name in ("home_turn_on", "home_set", "home_activate_scene"):
        schema = tools[name].schema
        assert schema.side_effect and schema.idempotency_key_template
    assert "light" in tools["home_turn_on"].schema.json_schema["properties"]["device_type"]["enum"]
    assert "color_temp" not in tools["home_set"].schema.json_schema["properties"]


async def test_turn_on_by_type_uses_the_speakers_room(tools, executor) -> None:
    result = await _call(tools, "home_turn_on", device_type="climate")

    assert result.ok is True
    assert result.content["completed"] is True
    assert result.content["message"] == "已打开客厅空调"
    assert result.metadata == {"external_action": True, "completed": True}
    assert executor.commands == [("living.ac", "on_off", "on", {})]
    assert executor.requests[0].origin.kind == "voice"


async def test_text_input_is_stamped_as_text(tools, executor) -> None:
    ctx = _ctx()
    ctx.input_modality = "text"

    await _call(tools, "home_turn_on", ctx, name="电视")

    assert executor.requests[0].origin.kind == "text"


async def test_partial_success_is_not_completed(tools, executor) -> None:
    executor.offline.add("master.bedside")

    result = await _call(tools, "home_turn_off", device_type="light", all=True)

    assert (result.ok, result.error_code) == (False, "partial_completed")
    assert result.content["completion"] == "partial"
    assert [d["status"] for d in result.content["devices"]] == ["succeeded", "succeeded", "failed"]


async def test_turn_off_maps_per_device_type(tools, executor) -> None:
    result = await _call(tools, "home_turn_off", area="客厅", device_type="cover")

    assert result.ok
    assert executor.commands == [("living.curtain", "position", "close", {})]


async def test_several_matches_are_ambiguous_unless_all(tools, executor) -> None:
    result = await _call(tools, "home_turn_on", _ctx(device_id=None), device_type="light")

    assert (result.ok, result.error_code) == (False, "ambiguous")
    assert [c["name"] for c in result.content["candidates"]] == ["客厅主灯", "主卧灯", "床头灯"]
    assert executor.requests == []

    result = await _call(
        tools, "home_turn_off", _ctx(device_id=None), device_type="light", all=True
    )
    assert result.ok and len(executor.commands) == 3


async def test_set_takes_exactly_one_setting(tools, executor) -> None:
    result = await _call(tools, "home_set", name="客厅主灯", brightness=40)
    assert (result.ok, result.content["message"]) == (True, "客厅主灯 已调到 40%")

    result = await _call(tools, "home_set", name="客厅主灯", brightness=40, volume=10)
    assert (result.ok, result.error_code) == (False, "invalid_arguments")


async def test_set_refused_by_the_vocabulary_is_not_completed(tools, executor) -> None:
    result = await _call(tools, "home_set", name="客厅空调", temperature=40)

    assert result.ok is False
    assert result.error_message == "客厅空调 只能设在 16–30°C"
    assert result.content["devices"][0]["status"] == "rejected"
    assert executor.requests == []


async def test_adjust_steps_relative(tools, executor) -> None:
    result = await _call(
        tools, "home_adjust", name="主卧空调", setting="temperature", direction="down", amount=2
    )

    assert result.content["message"] == "主卧空调 已调到 24°C"
    assert executor.commands == [("master.ac", "thermostat", "step", {"delta": -2})]


async def test_get_state_lists_the_home_without_arguments(tools, directory) -> None:
    result = await _call(tools, "home_get_state")

    assert result.ok
    assert len(result.content["devices"]) == 18
    assert result.content["scenes"] == ["回家", "离家", "观影", "睡眠"]
    first = result.content["devices"][0]
    assert (first["name"], first["area"], first["summary"]) == ("客厅主灯", "客厅", "客厅主灯 关着")


async def test_get_state_reports_every_match(tools) -> None:
    result = await _call(tools, "home_get_state", _ctx(device_id=None), device_type="climate")

    assert [d["device_id"] for d in result.content["devices"]] == ["living.ac", "master.ac"]


async def test_activate_scene_by_name(tools, executor) -> None:
    result = await _call(tools, "home_activate_scene", scene="观影模式")
    assert (result.ok, result.content["message"]) == (True, "已执行观影模式")
    assert executor.requests[0].scene_id == "scene.movie"
    assert [d["device_id"] for d in result.content["devices"]] == [
        "living.main_light",
        "living.curtain",
        "living.tv",
    ]

    result = await _call(tools, "home_activate_scene", scene="派对")
    assert (result.ok, result.error_code) == (False, "not_found")
    assert result.content == {"scenes": ["回家", "离家", "观影", "睡眠"]}


async def test_retried_call_is_one_logical_action(tools, executor) -> None:
    await _call(tools, "home_turn_on", call_id="call-a", name="电视")
    await _call(tools, "home_turn_on", call_id="call-b", name="电视")

    first, second = (r.request_id for r in executor.requests)
    assert first == second
    assert first.startswith("llm:")


async def test_unknown_names_and_unreachable_runtime(tools, directory) -> None:
    result = await _call(tools, "home_turn_on", name="投影仪")
    assert (result.ok, result.error_code) == (False, "not_found")

    result = await _call(tools, "home_turn_on")
    assert result.error_code == "invalid_arguments"

    directory.unavailable = True
    result = await _call(tools, "home_turn_on", name="电视")
    assert (result.ok, result.error_code, result.error_message) == (
        False,
        "smarthome_unavailable",
        "暂时连不上家居服务",
    )
