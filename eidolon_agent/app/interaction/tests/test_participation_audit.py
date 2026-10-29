import asyncio
import json
import stat
import pytest
from eidolon_agent.app.interaction.coordination import audit
from eidolon_agent.app.interaction.tests.test_coordinated_replies import bridge, task, permit

pytestmark = pytest.mark.unit

@pytest.mark.parametrize('cancel', [False, True])
async def test_reply_audit_separates_generated_from_played(tmp_path, monkeypatch, cancel):
    directory = tmp_path / 'audit'
    monkeypatch.setenv('EIDOLON_PARTICIPATION_AUDIT_DIR', str(directory))
    async def present(member, prepared, stream, grant):
        async for _ in stream:
            if cancel:
                raise asyncio.CancelledError()
        return True
    reply, member, _ = bridge(present)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await reply(member, task(), permit())
    else:
        assert (await reply(member, task(), permit())).completed
    files=list(directory.glob('*.jsonl'))
    record=json.loads(files[0].read_text())
    assert record['decision_id']=='decision' and record['turn_id']=='turn'
    assert record['generated_text']=='你好'
    assert record['playback_completed'] is (not cancel)
    assert record['text_is_audible_transcript'] is False
    assert record['outcome']==('CancelledError' if cancel else 'completed')
    assert stat.S_IMODE(files[0].stat().st_mode)==0o600

def test_logging_failure_does_not_escape_and_disabled_does_not_write(tmp_path, monkeypatch):
    monkeypatch.delenv('EIDOLON_PARTICIPATION_AUDIT_DIR', raising=False)
    audit.record('test')
    assert not list(tmp_path.iterdir())
    blocked=tmp_path/'file';blocked.write_text('not a directory')
    monkeypatch.setenv('EIDOLON_PARTICIPATION_AUDIT_DIR',str(blocked))
    before=audit.failures
    audit.record('test')
    assert audit.failures==before+1
