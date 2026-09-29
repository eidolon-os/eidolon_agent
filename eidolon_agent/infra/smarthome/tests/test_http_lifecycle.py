"""Reuse transports without crossing ownership or replaying device writes."""
import httpx
import pytest

from eidolon_agent.infra.smarthome.hub import HubSmartHomeClient


class TrackedTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = 0
        self.closed = 0

    async def handle_async_request(self, request):
        self.calls += 1
        return httpx.Response(200, json={'ok': True})

    async def aclose(self):
        self.closed += 1


@pytest.mark.asyncio
async def test_home_pool_reused_and_only_its_owner_closes_it(monkeypatch):
    transport = TrackedTransport()
    pool = httpx.AsyncClient(transport=transport)
    constructed = []

    def factory(**kwargs):
        constructed.append(kwargs)
        return pool

    monkeypatch.setattr('eidolon_agent.infra.smarthome.hub.create_async_client', factory)
    owned = HubSmartHomeClient(base_url='http://hub', token='token')
    await owned._post('snapshot', {})
    await owned._post('snapshot', {})
    assert len(constructed) == 1 and transport.calls == 2 and transport.closed == 0
    borrowed = HubSmartHomeClient(base_url='http://hub', token='another', client=pool)
    await borrowed.aclose()
    assert not pool.is_closed
    await owned.aclose()
    assert pool.is_closed and transport.closed == 1
