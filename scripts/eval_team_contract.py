"""Public synthetic scenes: real reply LLM, only decision inference is a fixture.

This verifies decision->role generation->public history. The presentation sink
collects text; it is NOT hardware/TTS playout evidence. No Owner data or devices
are read or operated. The exact same fixture HTTP app can be served separately
with scripts.participation_fixture for actual Channel/device acceptance.
"""
import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from eidolon_sdk.biz.control.coordination import CoordinationSelection
from eidolon_sdk.biz.persona import (
    PERSONA_GENOME_SCHEMA,
    PERSONA_REALIZER,
    build_default_persona_genome,
    persona_genome_hash,
)
from eidolon_sdk.device_foundation.v1.testing import named_device_instance_id

from eidolon_agent.app.interaction.coordination.application import IpTeamApplication
from eidolon_agent.app.runtime.bootstrap import _build_llm_router
from eidolon_agent.config import load_settings
from eidolon_agent.core.types.companion_runtime import CompanionRuntimeFacts
from eidolon_agent.core.types.turn import TurnEventKind
from eidolon_agent.infra.participation import HttpParticipationDecision
from scripts.participation_fixture import fixture_app

CASES = [
    dict(name='addressed', text='只请猪八戒说说周末去哪里玩。', expected=['b'], outcome='finished', rules=[
        dict(trigger_kind='user', action='respond', speaker_role='猪八戒', instruction='用一两句给出出游建议。'),
        dict(trigger_kind='companion', trigger_role='猪八戒', action='finish')]),
    dict(name='handoff', text='悟空提一个出游建议，八戒评价这个建议。', expected=['a', 'b'], outcome='finished', rules=[
        dict(trigger_kind='user', action='respond', speaker_role='孙悟空', instruction='用一两句提出出游建议。'),
        dict(trigger_kind='companion', trigger_role='孙悟空', action='respond', speaker_role='猪八戒', instruction='评价上一位刚刚说出的具体建议，不另起话题。'),
        dict(trigger_kind='companion', trigger_role='猪八戒', action='finish')]),
    dict(name='clarify', text='让他接着说。', expected=['b'], outcome='clarification', rules=[
        dict(trigger_kind='user', action='clarify', speaker_role='猪八戒', instruction='询问用户希望哪位成员继续，不要替用户选择。')]),
    dict(name='wait', text='我先想想，你们先别说。', expected=[], outcome='waiting', rules=[
        dict(trigger_kind='user', action='wait')]),
    dict(name='finish', text='这轮到此为止。', expected=[], outcome='finished', rules=[
        dict(trigger_kind='user', action='finish')]),
    dict(name='unknown', text='本测试没有预设这个输入。', expected=[], outcome='abstained', rules=[]),
]


async def evaluate(config):
    settings = load_settings(yaml_path=config)
    llm = _build_llm_router(settings)
    if llm.model_id == 'fake':
        raise RuntimeError('real reply model required')
    roles = {'a': '孙悟空', 'b': '猪八戒'}
    facts = {}
    for key, name in roles.items():
        genome = build_default_persona_genome(name=name)
        facts[key] = CompanionRuntimeFacts('team-eval', key, 'unused-memory', 'eval-genome', 1,
            PERSONA_GENOME_SCHEMA, persona_genome_hash(genome), PERSONA_REALIZER, genome, {})
    async def resolve(owner_id, companion_id):
        if owner_id != 'team-eval':
            raise ValueError('wrong test scope')
        return facts[companion_id]
    def ref(key):
        return dict(device_instance_id=named_device_instance_id('eval-' + key),
            owner_domain_id='owner-eval-domain', owner_domain_generation=1, claim_generation=1, trust_epoch=1)
    evidence = []
    for case in CASES:
        decision = HttpParticipationDecision('http://fixture/v1/participation/decide',
            transport=httpx.ASGITransport(app=fixture_app([
                dict(user_text=case['text'], **rule) for rule in case['rules']])))
        application = IpTeamApplication(llm=llm, runtime_authority=SimpleNamespace(resolve=resolve), decide=decision)
        turns = []
        async def present(member, request, stream, permit, turns=turns):
            chunks = []
            async for event in stream:
                permit.check()
                if event.kind is TurnEventKind.DELTA:
                    chunks.append(event.data.get('text', ''))
            turns.append(dict(speaker=member.companion_id, action=request.action,
                              instruction=request.instruction, text=''.join(chunks)))
            return True
        async def stop(member, epoch):
            pass  # No device was opened; explicit text-only evaluation sink.
        async def transcribe(capture, text=case['text']):
            return text  # Already committed text supplied by this evaluation.
        selected = CoordinationSelection.model_validate(dict(scenario='ip_role_group',
            session_id='eval-' + case['name'], input_device=ref('input'), reply_budget=4,
            goal='简短讨论周末出游，只表达当前成员自己的观点。', members=[
                dict(companion_id=key, output_device=ref(key), role=dict(name=name))
                for key, name in reversed(tuple(roles.items()))]))
        prepared = await application.prepare(selected, authenticated_owner_id='team-eval',
            present=present, stop=stop, transcribe=transcribe)
        session = prepared.session
        try:
            source = selected.input_device.device_instance_id
            session.press(device_id=source, capture_id=case['name'])
            await session.release(device_id=source, capture_id=case['name'])
            passed = ([t['speaker'] for t in turns] == case['expected']
                      and session.outcome == case['outcome'] and session.state == 'waiting')
            evidence.append(dict(case=case['name'], input=case['text'], turns=turns,
                outcome=session.outcome, error=session.error_code, orchestration_pass=passed,
                public_history=[m.model_dump(mode='json') for m in session.history]))
        finally:
            await session.close()
    return dict(reply_model=llm.model_id, decision_model='fixture-no-model',
                presentation='text_sink_not_hardware', cases=evidence)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--export-cases', type=Path)
    args = parser.parse_args()
    if args.export_cases:
        args.export_cases.write_text(json.dumps([dict(user_text=c['text'], **r)
            for c in CASES for r in c['rules']], ensure_ascii=False, indent=2))
    result = asyncio.run(evaluate(args.config))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'cases': len(result['cases']),
                      'orchestration_pass': sum(c['orchestration_pass'] for c in result['cases']),
                      'output': str(args.output)}))
    raise SystemExit(0 if all(c['orchestration_pass'] for c in result['cases']) else 1)
