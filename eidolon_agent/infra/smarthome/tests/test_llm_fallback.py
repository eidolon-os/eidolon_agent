"""The home LLM can propose, but never calls an actuator itself."""

from __future__ import annotations

from eidolon_sdk.biz.interpretation import Candidate, InterpretationRequest

from eidolon_agent.infra.llm.providers.fake import FakeLLM
from eidolon_agent.infra.smarthome.llm_fallback import LlmHomeFallback


async def test_llm_fallback_returns_one_structured_proposal() -> None:
    llm = FakeLLM(script=[{
        "kind": "tool_call", "name": "propose_home_action",
        "arguments": {"proposal": {
            "intent": "control", "target_status": "resolved", "targets": ["living.tv"],
            "action": {"trait": "on_off", "command": "on", "slots": []},
        }},
    }], per_token_delay_s=0)
    request = InterpretationRequest(
        interpretation_id="turn-1", domain="smarthome", utterance="打开电视",
        candidates=(Candidate(ref="living.tv", name="电视", kind="media"),), timeout_ms=1000,
    )

    proposal = await LlmHomeFallback(llm).propose(request)

    assert proposal is not None
    assert proposal.targets == ("living.tv",)
    assert (proposal.action.trait, proposal.action.command) == ("on_off", "on")
    assert llm.calls == 1
