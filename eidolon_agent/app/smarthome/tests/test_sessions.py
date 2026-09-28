import pytest

from eidolon_agent.app.smarthome.sessions import HomeSessions, HomeSessionUnavailable


def test_context_scoped_by_owner_device_and_voice_session():
    store = HomeSessions()
    initial = store.get("owner", "device", "session")
    initial.context.remember("关灯", None, question="哪盏？")
    assert store.get("owner", "device", "session") is initial
    for scope in [("other", "device", "session"), ("owner", "other", "session"), ("owner", "device", "new")]:
        assert store.get(*scope).context.snapshot() is None
    store.close("owner", "device", "session")
    assert not initial.context.active
    assert initial.context.snapshot() is None
    with pytest.raises(HomeSessionUnavailable):
        store.get("owner", "device", "session")


def test_bounded_storage_expires_abandoned_sessions_without_evicting_active_work():
    store = HomeSessions(ttl_s=60, capacity=1)
    old = store.get("o", "d", "s")
    with pytest.raises(HomeSessionUnavailable):
        store.get("o", "d", "other")
    old.touched -= 61
    new = store.get("o", "d", "new")
    assert not old.context.active
    assert new.context.active
