from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from eidolon_agent.app.smarthome.application import build_smart_home_application
from eidolon_agent.config.settings import SmartHomeSettings

_LAYA = {"interpreter": "laya", "laya": {"url": "http://127.0.0.1:8771"}}


@pytest.mark.parametrize(
    "smarthome",
    [
        {"interpreter": "laya"},  # Laya with nowhere to reach it
        {"laya": {"url": "http://127.0.0.1:8771", "continuation": True}},  # continuation on rules
        {"interpreter": "laya", "laya": {"url": "http://127.0.0.1:8771/v1/systemone"}},  # a route
    ],
)
def test_an_inconsistent_smarthome_section_is_refused(smarthome):
    with pytest.raises(ValidationError):
        SmartHomeSettings.model_validate(smarthome)


def test_continuation_needs_a_real_llm_before_any_client_is_created(monkeypatch):
    monkeypatch.setenv("EIDOLON_HUB_SMARTHOME_TOKEN", "t" * 32)
    settings = SmartHomeSettings.model_validate(
        {**_LAYA, "laya": {**_LAYA["laya"], "continuation": True}}
    )
    with pytest.raises(ValueError, match="real LLM"):
        build_smart_home_application(
            SimpleNamespace(model_id="fake"), runtime_authority=object(), settings=settings
        )


@pytest.mark.parametrize("continuation", [False, True])
async def test_continuation_is_explicit_opt_in_and_shares_laya_transport(monkeypatch, continuation):
    monkeypatch.setenv("EIDOLON_HUB_SMARTHOME_TOKEN", "t" * 32)
    settings = SmartHomeSettings.model_validate(
        {**_LAYA, "laya": {**_LAYA["laya"], "continuation": continuation}}
    )
    app = build_smart_home_application(
        SimpleNamespace(model_id="real"), runtime_authority=object(), settings=settings
    )
    try:
        port = app._command._continuation
        assert (port is not None) == continuation
        if port is not None:
            assert port._laya is app._laya
    finally:
        await app.close()
    assert app._laya._client.is_closed


async def test_rules_is_the_default_and_opens_no_laya_client(monkeypatch):
    monkeypatch.setenv("EIDOLON_HUB_SMARTHOME_TOKEN", "t" * 32)
    app = build_smart_home_application(SimpleNamespace(model_id="real"), runtime_authority=object())
    try:
        assert app._laya is None and app._command._continuation is None
    finally:
        await app.close()
