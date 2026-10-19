import asyncio

import pytest

from eidolon_agent.app.transport.grpc.channel_tools import ChannelToolExchange
from eidolon_agent.core.ports.channel_tool import ChannelToolRequest, ChannelToolResult


@pytest.mark.asyncio
async def test_exchange_accepts_only_matching_turn_and_name() -> None:
    sent: list[ChannelToolRequest] = []

    async def send(request: ChannelToolRequest) -> None:
        sent.append(request)

    async def cancel(_turn_id: str, _call_id: str) -> None:
        return None

    exchange = ChannelToolExchange(send, cancel, timeout_s=1)
    request = ChannelToolRequest("turn-1", "call-1", "music.status", {})
    task = asyncio.create_task(exchange.execute(request))
    await asyncio.sleep(0)
    assert sent == [request]
    assert not exchange.accept(ChannelToolResult("turn-2", "call-1", "music.status", True, {}))
    assert exchange.accept(ChannelToolResult("turn-1", "call-1", "music.status", True, {}))
    result = await task

    assert result.ok
    assert result.call_id == "call-1"


@pytest.mark.asyncio
async def test_exchange_rejects_call_id_reuse_with_different_arguments() -> None:
    sent: list[ChannelToolRequest] = []

    async def send(request: ChannelToolRequest) -> None:
        sent.append(request)

    async def cancel(_turn_id: str, _call_id: str) -> None:
        return None

    exchange = ChannelToolExchange(send, cancel, timeout_s=1)
    first = ChannelToolRequest("turn-1", "call-1", "music.play", {"song_id": "one"})
    first_task = asyncio.create_task(exchange.execute(first))
    await asyncio.sleep(0)
    exchange.accept(ChannelToolResult("turn-1", "call-1", "music.play", True, {}))
    await first_task

    replay = await exchange.execute(
        ChannelToolRequest("turn-1", "call-1", "music.play", {"song_id": "two"})
    )

    assert replay.error_code == "CHANNEL_TOOL_CALL_REPLAY"
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_exchange_scopes_replay_keys_to_turn() -> None:
    sent: list[ChannelToolRequest] = []

    async def send(request: ChannelToolRequest) -> None:
        sent.append(request)

    async def cancel(_turn_id: str, _call_id: str) -> None:
        return None

    exchange = ChannelToolExchange(send, cancel, timeout_s=1)
    first = ChannelToolRequest("turn-1", "call-1", "music.status", {})
    first_task = asyncio.create_task(exchange.execute(first))
    await asyncio.sleep(0)
    exchange.accept(ChannelToolResult("turn-1", "call-1", "music.status", True, {}))
    await first_task

    second = ChannelToolRequest("turn-2", "call-1", "music.status", {})
    second_task = asyncio.create_task(exchange.execute(second))
    await asyncio.sleep(0)
    assert len(sent) == 2
    exchange.accept(ChannelToolResult("turn-2", "call-1", "music.status", True, {}))

    assert (await second_task).ok


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [True, False])
async def test_cancellation_serializes_on_server_to_channel_event(timeout):
    from eidolon_sdk.grpc.eidolon_agent.v1 import eidolon_pb2 as pb

    events = []

    async def send(request):
        pass

    async def cancel(turn_id, call_id):
        event = pb.TurnEvent(
            kind=pb.TurnEvent.CHANNEL_TOOL_CANCEL,
            turn_id=turn_id,
            channel_tool_cancel=pb.ChannelToolCancel(turn_id=turn_id, call_id=call_id),
        )
        events.append(pb.TurnEvent.FromString(event.SerializeToString()))

    exchange = ChannelToolExchange(send, cancel, timeout_s=0.01 if timeout else 65)
    task = asyncio.create_task(exchange.execute(ChannelToolRequest("t", "c", "music.play", {})))
    if timeout:
        assert (await task).error_code == "CHANNEL_TOOL_TIMEOUT"
    else:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(events) == 1
    assert events[0].channel_tool_cancel.turn_id == "t"
    assert events[0].channel_tool_cancel.call_id == "c"


@pytest.mark.asyncio
async def test_disconnected_cancel_transport_preserves_timeout():
    async def send(request):
        pass

    async def cancel(turn_id, call_id):
        raise RuntimeError("stream closed")

    exchange = ChannelToolExchange(send, cancel, timeout_s=0.01)
    result = await exchange.execute(ChannelToolRequest("t", "c", "music.play", {}))
    assert result.error_code == "CHANNEL_TOOL_TIMEOUT"
