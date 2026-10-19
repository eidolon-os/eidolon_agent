from eidolon_agent.domain.tools.channel_music import build_channel_music_tools


class _Executor:
    async def execute(self, request):
        raise AssertionError(request)


def test_channel_music_tools_have_stable_names_and_side_effect_policy() -> None:
    tools = build_channel_music_tools(_Executor())
    by_name = {tool.schema.name: tool.schema for tool in tools}

    assert set(by_name) == {"music.search", "music.play", "music.stop", "music.status"}
    assert {schema.llm_name for schema in by_name.values()} == {
        "music_search",
        "music_play",
        "music_stop",
        "music_status",
    }
    assert all(
        "." not in schema.to_openai_function()["function"]["name"] for schema in by_name.values()
    )
    assert by_name["music.play"].side_effect
    assert by_name["music.stop"].side_effect
    assert not by_name["music.search"].side_effect
    assert not by_name["music.status"].side_effect
