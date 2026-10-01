from types import SimpleNamespace

import pytest

from eidolon_agent.app.smarthome.application import build_smart_home_application


@pytest.mark.parametrize(
    "mode,revision,model",
    [
        ("rules", "7b695ba8", "real"),
        ("laya", "45f3dedb", "real"),
        ("laya", "7b695ba8", "fake"),
    ],
)
def test_invalid_continuation_configuration_is_rejected_before_client_creation(
    monkeypatch, mode, revision, model
):
    monkeypatch.setenv("EIDOLON_HUB_SMARTHOME_TOKEN", "t" * 32)
    monkeypatch.setenv("EIDOLON_SMARTHOME_INTERPRETER", mode)
    monkeypatch.setenv("EIDOLON_SMARTHOME_LAYA_CONTINUATION_REVISION", revision)
    with pytest.raises(ValueError, match="continuation requires"):
        build_smart_home_application(SimpleNamespace(model_id=model), runtime_authority=object())


@pytest.mark.parametrize("revision", ["", "7b695ba8"])
async def test_continuation_is_explicit_opt_in_and_shares_laya_transport(monkeypatch, revision):
    monkeypatch.setenv("EIDOLON_HUB_SMARTHOME_TOKEN", "t" * 32)
    monkeypatch.setenv("EIDOLON_SMARTHOME_INTERPRETER", "laya")
    monkeypatch.setenv("EIDOLON_SMARTHOME_LAYA_CONTINUATION_REVISION", revision)
    app = build_smart_home_application(SimpleNamespace(model_id="real"), runtime_authority=object())
    try:
        port = app._command._continuation
        assert (port is not None) == bool(revision)
        if port is not None:
            assert port._laya is app._laya
    finally:
        await app.close()
    assert app._laya._client.is_closed
