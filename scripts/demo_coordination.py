"""Run the coordination scenarios without devices or model credentials.

ASR, reply generation, TTS completion and stop receipts are explicit simulators.
The session executor and participation contracts are the actual implementation.
Run: .venv/bin/python scripts/demo_coordination.py
"""

import asyncio
import json

from eidolon_agent.app.interaction.coordination import (
    CoordinationSession,
    Member,
    PlayedReply,
    Ports,
)
from eidolon_agent.app.interaction.coordination.mock_decision import MockDecision


async def scenario(name):
    events = []
    entered = asyncio.Event()
    release_playback = asyncio.Event()
    policy = MockDecision(
        ("stack-companion",) if name == "single" else ("stack-companion", "box-companion")
    )

    async def asr(capture):
        events.append({"simulated": "asr", "capture": capture})
        return "请介绍一下自己。" if capture == "first" else "停一下，换个问题。"

    async def reply(member, request, permit):
        permit.phase("thinking")
        events.append(
            {
                "simulated": "generation_started",
                "companion": member.companion_id,
                "source_kind": request.trigger.author_kind,
            }
        )
        if name == "interrupt" and request.trigger.message_id == "first":
            entered.set()
            try:
                await release_playback.wait()
            except asyncio.CancelledError:
                # Demonstrate rejection of a late external result after cancel.
                await release_playback.wait()
            permit.check()
        permit.phase("speaking")
        events.append({"simulated": "playback_completed", "device": member.device_id})
        return PlayedReply(f"{member.companion_id} 的模拟回复", True)

    async def stop(member, epoch):
        events.append({"simulated": "stop_dispatched", "device": member.device_id, "epoch": epoch})
        if name == "slow-stop" and member.device_id == "stackchan":
            await asyncio.Event().wait()
        events.append({"simulated": "stop_completed", "device": member.device_id, "epoch": epoch})

    session = CoordinationSession(
        session_id=f"demo-{name}",
        input_device_id="waveshare-2.06",
        members=(Member("stack-companion", "stackchan"), Member("box-companion", "box3")),
        ports=Ports(policy, reply, stop, asr),
        discussion=name == "discussion",
        reply_budget=4,
        stop_timeout=0.05,
    )
    try:
        session.press(device_id="waveshare-2.06", capture_id="first")
        task = session.release(device_id="waveshare-2.06", capture_id="first")
        if name == "interrupt":
            await asyncio.wait_for(entered.wait(), 1)
            session.press(device_id="waveshare-2.06", capture_id="second")
            assert session.state == "recording"
            release_playback.set()
            await asyncio.gather(task, return_exceptions=True)
            task = session.release(device_id="waveshare-2.06", capture_id="second")
        await task
        return {
            "scenario": name,
            "mode": "simulation-no-hardware-no-model",
            "state": session.state,
            "quarantined": dict(session.failures),
            "public_messages": [m.model_dump() for m in session.history],
            "execution": list(session.events),
            "adapter_events": events.copy(),
        }
    finally:
        release_playback.set()
        await session.close()


async def main():
    results = [
        await scenario(name)
        for name in ("single", "ordered", "discussion", "interrupt", "slow-stop")
    ]
    assert all(
        result["state"] == ("failed" if result["scenario"] == "slow-stop" else "waiting")
        for result in results
    )
    for result in results:
        replies = [m for m in result["public_messages"] if m["author_kind"] == "companion"]
        expected = {"single": 1, "ordered": 2, "discussion": 4, "interrupt": 2, "slow-stop": 0}
        assert len(replies) == expected[result["scenario"]]
    print(
        json.dumps(
            {"result": "passed", "hardware_latency_measured": False, "scenarios": results},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
