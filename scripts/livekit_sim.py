"""Simulate a LiveKit voice-pipeline caller against a running eidolon-agent.

Flow:
    1. POST /api/admin/pairing/codes to issue a code (no auth needed in dev)
    2. gRPC ExchangePairingCode → device_token
    3. Open Chat bidi stream (with bearer token) and send a turn
    4. Print every TurnEvent until DONE

Usage::

    python scripts/livekit_sim.py "你好"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

import grpc
import httpx

# Make the in-repo proto package importable without installing the wheel.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eidolon_agent.transport.grpc.proto import pb, pbg


async def main(text: str, http_base: str, grpc_target: str) -> None:
    async with httpx.AsyncClient() as http:
        # 1. Issue pairing code
        r = await http.post(
            f"{http_base}/api/admin/pairing/codes",
            json={"tenant_id": "demo", "user_id": "alice", "default_template_id": "caretaker_jiezhi"},
        )
        r.raise_for_status()
        code = r.json()["code"]
        print(f"issued pairing code: {code}")

        # 2. Ensure an agent instance exists for the user (idempotent-ish)
        await http.post(
            f"{http_base}/api/admin/agents",
            json={"template_id": "caretaker_jiezhi", "tenant_id": "demo", "user_id": "alice"},
        )

    # 3. Exchange code → token over gRPC (no auth needed)
    async with grpc.aio.insecure_channel(grpc_target) as channel:
        stub = pbg.EidolonAgentStub(channel)
        exch = await stub.ExchangePairingCode(
            pb.ExchangeRequest(pairing_code=code, device_id="dev-sim", device_name="LiveKit sim")
        )
        print(f"got device_token (len={len(exch.device_token)}); user_id={exch.user_id}")

        # 4. Open Chat bidi
        metadata = (("authorization", f"Bearer {exch.device_token}"),)

        async def _requests():
            yield pb.ChatRequest(
                start=pb.StartTurn(
                    turn_id=uuid.uuid4().hex,
                    conversation_id="conv-sim",
                    text=text,
                )
            )
            # Hold the stream open for a moment so we get all events.
            await asyncio.sleep(2.0)

        async for ev in stub.Chat(_requests(), metadata=metadata):
            kind = pb.TurnEvent.Kind.Name(ev.kind)
            data = dict(ev.data) if ev.data else {}
            print(f"  seq={ev.seq:>3}  {kind:<10}  {json.dumps(data, ensure_ascii=False)}")
            if kind in ("DONE", "ERROR"):
                break


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("text", nargs="?", default="你好")
    p.add_argument("--http", default="http://127.0.0.1:8081")
    p.add_argument("--grpc", default="127.0.0.1:50051")
    args = p.parse_args()
    asyncio.run(main(args.text, args.http, args.grpc))
