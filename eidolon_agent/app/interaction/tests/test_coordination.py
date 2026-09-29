import asyncio

import pytest
from eidolon_sdk.biz.participation import Proposal

from eidolon_agent.app.interaction.coordination import (
    CoordinationSession,
    Member,
    PlayedReply,
    Ports,
)
from tests.decision_helpers import decided

pytestmark = pytest.mark.unit


class Harness:
    def __init__(self, **options):
        self.log = []
        self.permits = []
        self.requests = []
        self.session = CoordinationSession(
            session_id="demo",
            input_device_id="waveshare",
            members=(Member("a", "stackchan"), Member("b", "box3")),
            ports=Ports(self.decide, self.reply, self.stop, self.asr),
            **options,
        )

    async def policy(self, request):
        if request.trigger.author_kind == 'user':
            return decided(request, speaker='a')
        if request.trigger.author_id == 'a':
            return decided(request, speaker='b')
        return decided(request, 'finish')

    async def decide(self, request):
        return await self.policy(request)

    async def reply(self, member, request, permit):
        self.permits.append(permit)
        self.requests.append(request)
        permit.check()
        self.log.append(("reply", member.companion_id))
        return PlayedReply(member.companion_id + " speaks", True)

    async def stop(self, member, epoch):
        self.log.append(("stop", member.device_id, epoch))

    async def asr(self, capture):
        self.log.append(("asr", capture))
        return "hello"

    def press(self, capture="one"):
        return self.session.press(device_id="waveshare", capture_id=capture)

    def release(self, capture="one"):
        return self.session.release(device_id="waveshare", capture_id=capture)


async def test_single_input_serial_replies_public_context_and_no_live_old_permits():
    h = Harness()
    try:
        epoch = h.press()
        assert h.press() == epoch
        await h.release()
        assert h.release() is None
        assert [x for x in h.log if x[0] == "asr"] == [("asr", "one")]
        assert [x for x in h.log if x[0] == "reply"] == [("reply", "a"), ("reply", "b")]
        assert h.requests[1].public_context.recent_messages[-1].author_id == "a"
        assert [m.author_kind for m in h.session.history] == ["user", "companion", "companion"]
        assert all(not p.current() for p in h.permits)
        assert h.session.state == "waiting"
        with pytest.raises(ValueError):
            h.session.press(device_id="box3", capture_id="bad")
        with pytest.raises(ValueError):
            h.press("one")
    finally:
        await h.session.close()


async def test_discussion_uses_companion_text_without_asr_and_stops_at_budget():
    h = Harness(reply_budget=5)
    async def continuous(request):
        return decided(request, speaker='b' if request.trigger.author_id == 'a' else 'a')
    h.policy = continuous
    try:
        h.press()
        await h.release()
        assert len([x for x in h.log if x[0] == "asr"]) == 1
        assert [x[1] for x in h.log if x[0] == "reply"] == ["a", "b", "a", "b", "a"]
        assert h.requests[2].trigger.author_kind == "companion"
        assert h.requests[2].trigger.author_id == "b"
        assert h.session.state == "waiting"
    finally:
        await h.session.close()


async def test_press_during_playback_revokes_output_and_drops_queued_member():
    h = Harness()
    started, finish = asyncio.Event(), asyncio.Event()

    async def reply(member, request, permit):
        h.permits.append(permit)
        h.log.append(("reply", member.companion_id))
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            # A misbehaving external model may still return after cancellation.
            await finish.wait()
        return PlayedReply("late old text", True)

    h.session.ports = Ports(h.decide, reply, h.stop, h.asr)
    try:
        h.press()
        old = h.release()
        await asyncio.wait_for(started.wait(), 1)
        h.press("two")
        assert h.session.state == "recording"
        assert not h.permits[0].current()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await old
        assert h.session.state == "recording"
        assert not any(x == ("reply", "b") for x in h.log)
        assert [m.text for m in h.session.history] == ["hello"]
    finally:
        finish.set()
        await h.session.close()


@pytest.mark.parametrize("stage", ["asr", "decision"])
async def test_new_press_does_not_wait_for_old_asr_or_decision(stage):
    h = Harness()
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed(value):
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
        return "stale" if stage == "asr" else await h.policy(value)

    h.session.ports = Ports(
        delayed if stage == "decision" else h.decide,
        h.reply,
        h.stop,
        delayed if stage == "asr" else h.asr,
    )
    try:
        h.press()
        old = h.release()
        await asyncio.wait_for(started.wait(), 1)
        h.press("two")
        assert h.session.state == "recording"
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await old
        assert not h.permits
        assert h.session.state == "recording"
    finally:
        finish.set()
        await h.session.close()


async def test_slow_stop_is_parallel_and_unconfirmed_silence_blocks_new_output():
    h = Harness(stop_timeout=0.02)
    dispatched = set()

    async def stop(member, epoch):
        dispatched.add(member.device_id)
        if member.device_id == "stackchan":
            await asyncio.Event().wait()

    h.session.ports = Ports(h.decide, h.reply, stop, h.asr)
    try:
        h.press()
        assert h.session.state == "recording"
        await h.release()
        assert dispatched == {"stackchan", "box3"}
        assert "a" in h.session.failures
        assert not [x for x in h.log if x[0] == "reply"]
        assert h.session.state == "failed"
    finally:
        await h.session.close()


async def test_queue_waits_for_actual_playback_not_just_generation():
    h = Harness()
    first, played = asyncio.Event(), asyncio.Event()

    async def reply(member, request, permit):
        h.log.append(("reply", member.companion_id))
        if member.companion_id == "a":
            first.set()
            await played.wait()
        permit.check()
        return PlayedReply("finished", True)

    h.session.ports = Ports(h.decide, reply, h.stop, h.asr)
    try:
        h.press()
        task = h.release()
        await asyncio.wait_for(first.wait(), 1)
        assert [x for x in h.log if x[0] == "reply"] == [("reply", "a")]
        played.set()
        await task
        assert [x for x in h.log if x[0] == "reply"] == [("reply", "a"), ("reply", "b")]
    finally:
        played.set()
        await h.session.close()


@pytest.mark.parametrize("invalid", ["stale", "outsider", "unfinished"])
async def test_invalid_decision_or_unfinished_playback_fails_closed(invalid):
    h = Harness()

    async def decide(request):
        result = await h.policy(request)
        if invalid == "stale":
            return result.model_copy(update={"cancellation_epoch": request.cancellation_epoch - 1})
        return result.model_copy(
            update={"proposal": Proposal(action="respond", participants=("intruder",))}
        )

    async def reply(member, request, permit):
        return PlayedReply("not heard fully", False)

    h.session.ports = Ports(h.decide if invalid == "unfinished" else decide, reply, h.stop, h.asr)
    try:
        h.press()
        await h.release()
        assert h.session.state == "failed"
        assert not any(m.author_kind == "companion" for m in h.session.history)
    finally:
        await h.session.close()


async def test_stop_is_dispatched_to_all_before_any_slow_receipt():
    h = Harness()
    release_slow, fast_stopped = asyncio.Event(), asyncio.Event()

    async def stop(member, epoch):
        if member.device_id == "stackchan":
            await release_slow.wait()
        else:
            fast_stopped.set()

    h.session.ports = Ports(h.decide, h.reply, stop, h.asr)
    try:
        h.press()
        await asyncio.wait_for(fast_stopped.wait(), 1)
        assert not release_slow.is_set()
        assert h.session.state == "recording"
    finally:
        release_slow.set()
        await h.session.close()


async def test_older_stop_must_drain_before_new_output_can_start():
    h = Harness()
    old_dispatched, drain = asyncio.Event(), asyncio.Event()

    async def stop(member, epoch):
        if epoch == 1:
            old_dispatched.set()
            await drain.wait()

    h.session.ports = Ports(h.decide, h.reply, stop, h.asr)
    try:
        h.press()
        await asyncio.wait_for(old_dispatched.wait(), 1)
        h.press("two")
        task = h.release("two")
        # ASR and later stop receipts can complete; the older stop is outstanding.
        for _ in range(5):
            await asyncio.sleep(0)
        assert not h.permits
        drain.set()
        await task
        assert len(h.permits) == 2
    finally:
        drain.set()
        await h.session.close()


async def test_only_current_speaker_can_update_state_and_old_phase_is_rejected():
    h = Harness()

    async def reply(member, request, permit):
        if member.companion_id == "a":
            assert h.session.member_states == {"a": "thinking", "b": "waiting"}
        else:
            assert h.session.member_states == {"a": "waiting", "b": "thinking"}
            with pytest.raises(asyncio.CancelledError):
                h.permits[0].phase("speaking")
        permit.phase("speaking")
        assert h.session.member_states[member.companion_id] == "speaking"
        h.permits.append(permit)
        return PlayedReply("done", True)

    h.session.ports = Ports(h.decide, reply, h.stop, h.asr)
    try:
        h.press()
        await h.release()
        assert h.session.member_states == {"a": "waiting", "b": "waiting"}
    finally:
        await h.session.close()


async def test_empty_ptt_still_stops_every_device_and_does_not_decide():
    h = Harness()

    async def empty(capture):
        return " "

    async def forbidden(request):
        raise AssertionError("empty capture must not cause a decision")

    h.session.ports = Ports(forbidden, h.reply, h.stop, empty)
    try:
        h.press()
        await h.release()
        assert {x[1] for x in h.log if x[0] == "stop"} == {"stackchan", "box3"}
        assert not h.session.history
        assert h.session.state == "waiting"
    finally:
        await h.session.close()


async def test_close_revokes_current_output_and_rejects_new_input():
    h = Harness()
    started = asyncio.Event()

    async def reply(member, request, permit):
        h.permits.append(permit)
        started.set()
        await asyncio.Event().wait()

    h.session.ports = Ports(h.decide, reply, h.stop, h.asr)
    h.press()
    task = h.release()
    await asyncio.wait_for(started.wait(), 1)
    await h.session.close()
    assert h.session.state == "closed"
    assert not h.permits[0].current()
    assert task.cancelled()
    with pytest.raises(ValueError):
        h.press("after-close")
    await h.session.close()


async def test_playout_longer_than_decision_deadline_still_advances_with_context():
    h = Harness(stage_timeout=0.01)
    original = h.reply
    async def long_reply(member, request, permit):
        await asyncio.sleep(0.04)  # Normal speech can outlast an ASR/decision RPC.
        return await original(member, request, permit)
    from dataclasses import replace
    h.session.ports = replace(h.session.ports, reply=long_reply)
    try:
        h.press()
        await h.release()
        assert [x for x in h.log if x[0] == 'reply'] == [('reply', 'a'), ('reply', 'b')]
        assert h.requests[1].public_context.recent_messages[-1].author_id == 'a'
        assert h.session.state == 'waiting'
    finally:
        await h.session.close()


async def test_late_failed_round_cannot_publish_current_capture_completion():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from eidolon_agent.app.transport.coordination import CoordinationStream
    # A provider may convert cancellation into an ordinary failure. Even then,
    # its done callback has no authority over the newer capture's UI.
    stream = CoordinationStream(None)
    stream._capture_id = 'new'
    stream.session = SimpleNamespace(state='transcribing', member_states={},
                                     epoch=2, outcome='waiting', error_code='')
    stream.emit = Mock()
    old = SimpleNamespace(cancelled=lambda: False)
    stream._round_done(old, 'old')
    stream.emit.assert_not_called()


async def test_new_ptt_recovers_only_after_fresh_silence_confirmation():
    h = Harness()
    fail = True
    async def stop(member, epoch):
        if fail:
            raise RuntimeError("TEAM_STOP_REJECTED")
    h.session.ports = Ports(h.decide, h.reply, stop, h.asr)
    try:
        h.press()
        await h.release()
        assert h.session.error_code == "TEAM_STOP_UNCONFIRMED"
        assert not [x for x in h.log if x[0] == "reply"]
        fail = False
        h.press("retry")
        await h.release("retry")
        assert not h.session.failures
        assert [x for x in h.log if x[0] == "reply"]
    finally:
        await h.session.close()


async def test_obsolete_stop_failure_cannot_poison_current_epoch():
    h = Harness()
    async def stop(member, epoch):
        if epoch == 1:
            raise RuntimeError("old failure")
    h.session.ports = Ports(h.decide, h.reply, stop, h.asr)
    try:
        h.session.epoch = 2
        await h.session._stop_all(1)
        assert not h.session.failures
    finally:
        await h.session.close()


async def test_late_llm_fallback_cannot_speak_after_new_user_press():
    from eidolon_agent.app.interaction.coordination.decision import FallbackParticipationDecision
    from eidolon_sdk.biz.participation import DecisionResult, Snapshot
    h=Harness()
    entered=asyncio.Event();release=asyncio.Event()
    async def primary(req):
        return DecisionResult(**{k:getattr(req,k) for k in Snapshot.model_fields},
                              status='abstained',policy_version='test',model_version='small')
    async def fallback(req):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # Simulate a provider returning after cancellation.
        return decided(req,speaker='a')
    h.policy=FallbackParticipationDecision(primary,fallback,primary_timeout_ms=100)
    try:
        h.press('old')
        task=h.release('old')
        await entered.wait()
        h.press('new')
        release.set()
        await asyncio.gather(task,return_exceptions=True)
        assert not h.permits and not [r for r in h.log if r[0]=='reply']
    finally:
        release.set()
        await h.session.close()
