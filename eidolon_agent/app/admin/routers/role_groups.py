"""Authenticated Host-internal transport for a pre-authorized IP role group.

This is not a Mobile endpoint. The upstream service must validate current device
lifecycles/capabilities and reserve physical devices before opening this stream.
Only Companion authorization lives in Agent. Channel must emergency-stop its
prepared endpoints when this connection closes; a dead socket cannot deliver a
physical stop. A new connection never resumes old output.
"""

import asyncio

import anyio
from eidolon_sdk.biz.control.coordination_stream import CLIENT_FRAME, MAX_FRAME_BYTES, OpenScene
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES
from eidolon_agent.app.transport.coordination import CoordinationStream
from eidolon_agent.core.errors import PermissionDeniedError

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)


class SceneConnections:
    """Agent-side stream exclusivity; Provider still owns all device reservations."""

    def __init__(self):
        self.active = set()
        self.devices = set()

    def reserve(self, opened):
        key = (opened.owner_id, opened.selection.session_id)
        devices = {ref.device_instance_id for ref in opened.selection.devices}
        if len(self.active) >= 64 or key in self.active or devices & self.devices:
            raise ValueError("role-group stream or device is already in use")
        self.active.add(key)
        self.devices.update(devices)

    def release(self, opened):
        self.active.discard((opened.owner_id, opened.selection.session_id))
        self.devices.difference_update(ref.device_instance_id for ref in opened.selection.devices)


async def read_frame(socket):
    raw = await socket.receive_text()
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        raise ValueError("role-group frame too large")
    return raw


@router.websocket("/role-groups/stream")
async def role_group_stream(socket: WebSocket):
    await socket.accept()
    bridge = None
    opened = None
    tasks = []
    reserved = False
    connections = socket.app.state.role_group_connections
    try:
        async with asyncio.timeout(10):
            opened = OpenScene.model_validate_json(await read_frame(socket))
            connections.reserve(opened)
            reserved = True
            bridge = CoordinationStream(opened)
            await bridge.prepare(
                registry=socket.app.state.agent_registry,
                runtime_authority=socket.app.state.runtime_authority,
            )

        async def read():
            while True:
                bridge.accept(CLIENT_FRAME.validate_json(await read_frame(socket)))

        tasks = [
            asyncio.create_task(read()),
            asyncio.create_task(bridge.write_to(socket.send_json)),
            asyncio.create_task(bridge.closed.wait()),
        ]
        async with asyncio.timeout(3600):
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        await socket.close(code=1000)
    except WebSocketDisconnect:
        pass
    except (ValueError, ValidationError, PermissionDeniedError):
        await socket.close(code=1008, reason="invalid or unauthorized role-group stream")
    except TimeoutError:
        await socket.close(code=1008, reason="role-group stream deadline exceeded")
    finally:
        # ASGI servers can cancel a socket task during shutdown. Revoke permits
        # and drop this registration even under that cancellation scope.
        with anyio.CancelScope(shield=True):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                if bridge is not None:
                    await bridge.disconnect()
            finally:
                if reserved:
                    connections.release(opened)
