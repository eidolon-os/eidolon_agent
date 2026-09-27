"""Test-only results at the model port. No runtime scheduling policy."""
from eidolon_sdk.biz.participation import DecisionResult, Proposal, Snapshot


def decided(request, action='respond', speaker=None, instruction=''):
    return DecisionResult(
        **{key: getattr(request, key) for key in Snapshot.model_fields},
        status='decided',
        proposal=Proposal(action=action, participants=(speaker,) if speaker else (),
                          instruction=instruction),
        policy_version='test-input-v2', model_version='fixture-no-model',
    )
