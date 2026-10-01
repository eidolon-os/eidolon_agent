import pytest
from eidolon_sdk.biz.smarthome import HomeSessionScope

from eidolon_agent.app.smarthome.sessions import HomeSessions, HomeSessionUnavailable


def scope(**changes):
    return HomeSessionScope.model_validate({
        "owner_id": "owner", "companion_id": "companion",
        "device_ref": "device", "session_id": "session", **changes,
    })


def test_context_scoped_by_owner_device_and_voice_session():
    store = HomeSessions()
    initial = store.get(scope())
    initial.context.remember("关灯", None, question="哪盏？")
    assert store.get(scope()) is initial
    for changes in [{"owner_id": "other"}, {"device_ref": "other"}, {"session_id": "new"}]:
        assert store.get(scope(**changes)).context.snapshot() is None
    store.close(scope())
    assert not initial.context.active
    assert initial.context.snapshot() is None
    with pytest.raises(HomeSessionUnavailable):
        store.get(scope())


def test_bounded_storage_expires_abandoned_sessions_without_evicting_active_work():
    store = HomeSessions(ttl_s=60, capacity=1)
    old = store.get(scope())
    with pytest.raises(HomeSessionUnavailable):
        store.get(scope(session_id="other"))
    old.touched -= 61
    new = store.get(scope(session_id="new"))
    assert not old.context.active
    assert new.context.active


def test_companion_cannot_change_or_close_another_companions_session():
    store = HomeSessions()
    initial = store.get(scope())
    initial.context.remember("关灯", None, question="哪盏？")
    changed = scope(companion_id="other")
    with pytest.raises(HomeSessionUnavailable, match="new home session"):
        store.get(changed)
    with pytest.raises(HomeSessionUnavailable, match="does not match"):
        store.close(changed)
    assert initial.context.active and initial.context.snapshot() is not None
    assert store.get(scope(companion_id="other", session_id="new")).context.snapshot() is None
