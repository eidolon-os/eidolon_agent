"""End-to-end Chat latency bench for P0 diagnostics.

Drives a *real* gRPC ``EidolonAgent.Chat`` bidi against a running brain and
times each turn from ``StartTurn`` send to first ``DELTA`` and to ``DONE``.
The brain logs ``turn_timings`` / ``compile_timings`` / ``litellm_timings``
lines per turn; this script's table tells you the wire-observed numbers,
and you correlate against the brain log for the phase breakdown.

Default flow (the one we care about for P0):
    1. Issue a pairing code for a *fresh* (tenant, user) — first turn is a
       true cold start.
    2. Exchange code → device_token.
    3. Open Chat bidi, send ``--turns`` turns, time each.
    4. Print per-turn first_delta_ms / total_ms + summary stats.

Usage::

    python scripts/bench_chat.py --turns 5
    python scripts/bench_chat.py --turns 5 --user existing-user  # warm path
    python scripts/bench_chat.py --turns 5 --reuse-stream        # one bidi for all turns
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
import uuid

import httpx
from eidolon_sdk.grpc import authorization_metadata, create_aio_channel

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eidolon_agent.app.transport.grpc.proto import pb, pbg
from scripts.replay_live_service import ensure_registry_user

PROMPTS = [
    "你好，我刚醒来，今天感觉怎么样？",
    "帮我想想晚饭吃什么。",
    "讲个笑话。",
    "如果一个朋友最近情绪低落，怎么安慰？",
    "总结一下我们刚才聊了什么。",
    "再来一句鼓励的话。",
    "晚安。",
]


async def _issue_token(
    http: httpx.AsyncClient,
    grpc_target: str,
    http_base: str,
    tenant_id: str,
    user_id: str,
    template_id: str,
) -> tuple[str, str]:
    r = await http.post(
        f"{http_base}/api/admin/pairing/codes",
        json={
            "tenant_id": tenant_id,
            "user_id": user_id,
            "default_template_id": template_id,
        },
    )
    r.raise_for_status()
    code = r.json()["code"]

    async with create_aio_channel(grpc_target) as channel:
        stub = pbg.EidolonAgentStub(channel)
        exch = await stub.ExchangePairingCode(
            pb.ExchangeRequest(
                pairing_code=code,
                device_id=f"bench-{uuid.uuid4().hex[:8]}",
                device_name="bench_chat.py",
            )
        )
    return exch.device_token, exch.user_id


async def _bench_one_turn_per_stream(
    grpc_target: str, token: str, turns: int, conv_id: str
) -> list[dict]:
    """Open a fresh bidi stream per turn — matches the 'channel reconnects' case."""
    results = []
    metadata = authorization_metadata(token)
    for i in range(turns):
        prompt = PROMPTS[i % len(PROMPTS)]
        turn_id = uuid.uuid4().hex
        async with create_aio_channel(grpc_target) as channel:
            stub = pbg.EidolonAgentStub(channel)
            results.append(await _run_one_turn(stub, metadata, conv_id, turn_id, prompt))
    return results


async def _bench_reused_stream(
    grpc_target: str, token: str, turns: int, conv_id: str
) -> list[dict]:
    """Single bidi stream for N turns — matches the 'channel keeps connection open' case."""
    results = []
    metadata = authorization_metadata(token)

    async with create_aio_channel(grpc_target) as channel:
        stub = pbg.EidolonAgentStub(channel)

        send_queue: asyncio.Queue = asyncio.Queue()

        async def _request_gen():
            while True:
                item = await send_queue.get()
                if item is None:
                    return
                yield item

        call = stub.Chat(_request_gen(), metadata=metadata)

        for i in range(turns):
            prompt = PROMPTS[i % len(PROMPTS)]
            turn_id = uuid.uuid4().hex
            t_send = time.monotonic()
            await send_queue.put(
                pb.ChatRequest(
                    start=pb.StartTurn(
                        turn_id=turn_id, conversation_id=conv_id, text=prompt
                    )
                )
            )
            first_delta_ms: int | None = None
            done_ms: int | None = None
            error: str | None = None
            async for ev in call:
                kind = pb.TurnEvent.Kind.Name(ev.kind)
                if ev.turn_id != turn_id:
                    continue
                if kind == "DELTA" and first_delta_ms is None:
                    first_delta_ms = int((time.monotonic() - t_send) * 1000)
                elif kind == "DONE":
                    done_ms = int((time.monotonic() - t_send) * 1000)
                    break
                elif kind == "ERROR":
                    done_ms = int((time.monotonic() - t_send) * 1000)
                    error = dict(ev.data).get("message", "?")
                    break
            results.append(
                {
                    "turn": i + 1,
                    "turn_id": turn_id,
                    "first_delta_ms": first_delta_ms,
                    "total_ms": done_ms,
                    "error": error,
                }
            )

        await send_queue.put(None)

    return results


async def _run_one_turn(
    stub, metadata, conv_id: str, turn_id: str, prompt: str
) -> dict:
    t_send = time.monotonic()

    async def _request_gen():
        yield pb.ChatRequest(
            start=pb.StartTurn(turn_id=turn_id, conversation_id=conv_id, text=prompt)
        )
        # Hold the stream until we see DONE; the receive loop closes by breaking out.
        while True:
            await asyncio.sleep(3600)

    first_delta_ms: int | None = None
    done_ms: int | None = None
    error: str | None = None
    call = stub.Chat(_request_gen(), metadata=metadata)
    try:
        async for ev in call:
            kind = pb.TurnEvent.Kind.Name(ev.kind)
            if kind == "DELTA" and first_delta_ms is None:
                first_delta_ms = int((time.monotonic() - t_send) * 1000)
            elif kind == "DONE":
                done_ms = int((time.monotonic() - t_send) * 1000)
                break
            elif kind == "ERROR":
                done_ms = int((time.monotonic() - t_send) * 1000)
                error = dict(ev.data).get("message", "?")
                break
    finally:
        call.cancel()
    return {
        "turn": -1,
        "turn_id": turn_id,
        "first_delta_ms": first_delta_ms,
        "total_ms": done_ms,
        "error": error,
    }


def _print_table(results: list[dict]) -> None:
    print()
    print(f"{'#':>3}  {'first_delta_ms':>15}  {'total_ms':>10}  {'error':<30}")
    print("-" * 65)
    for i, r in enumerate(results, 1):
        first = r.get("first_delta_ms")
        total = r.get("total_ms")
        err = r.get("error") or ""
        print(
            f"{i:>3}  {first if first is not None else '-':>15}  "
            f"{total if total is not None else '-':>10}  {err[:30]:<30}"
        )
    deltas = [r["first_delta_ms"] for r in results if r.get("first_delta_ms") is not None]
    totals = [r["total_ms"] for r in results if r.get("total_ms") is not None]
    if deltas:
        print(
            f"\nfirst_delta_ms: min={min(deltas)} median={statistics.median(deltas):.0f} max={max(deltas)}"
        )
    if totals:
        print(
            f"total_ms:       min={min(totals)} median={statistics.median(totals):.0f} max={max(totals)}"
        )
    print()


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--http", default="http://127.0.0.1:8081", help="admin HTTP base")
    p.add_argument("--grpc", default="127.0.0.1:45051", help="gRPC target")
    p.add_argument(
        "--registry-http",
        default=None,
        help=(
            "Central eidolon_admin API base including /api, e.g. "
            "http://127.0.0.1:18765/api. Used with --provision-user."
        ),
    )
    p.add_argument(
        "--provision-user",
        action="store_true",
        help="Ensure the benchmark user exists through eidolon_admin /api/users before pairing.",
    )
    p.add_argument("--tenant", default="demo")
    p.add_argument(
        "--user",
        default=None,
        help="user_id; default = fresh uuid (cold-start scenario)",
    )
    p.add_argument("--template", default="caretaker_jiezhi")
    p.add_argument("--turns", type=int, default=5)
    p.add_argument(
        "--reuse-stream",
        action="store_true",
        help="send all turns over a single bidi stream (default: fresh stream per turn)",
    )
    p.add_argument(
        "--conv",
        default=None,
        help="conversation_id; default = fresh per run",
    )
    args = p.parse_args()

    user_id = args.user or f"bench-{uuid.uuid4().hex[:8]}"
    conv_id = args.conv or f"conv-{uuid.uuid4().hex[:8]}"
    print(
        f"tenant={args.tenant} user={user_id} conv={conv_id} turns={args.turns} "
        f"reuse_stream={args.reuse_stream}"
    )

    async with httpx.AsyncClient(trust_env=False) as http:
        if args.provision_user:
            provisioning = await ensure_registry_user(
                http=http,
                registry_base=args.registry_http,
                tenant_id=args.tenant,
                user_id=user_id,
            )
            print(f"provisioning={provisioning}")
        token, _ = await _issue_token(
            http,
            args.grpc,
            args.http,
            args.tenant,
            user_id,
            args.template,
        )

    if args.reuse_stream:
        results = await _bench_reused_stream(args.grpc, token, args.turns, conv_id)
    else:
        results = await _bench_one_turn_per_stream(args.grpc, token, args.turns, conv_id)
    _print_table(results)


if __name__ == "__main__":
    asyncio.run(main())
