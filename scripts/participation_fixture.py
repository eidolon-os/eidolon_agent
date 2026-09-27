"""Input-matched decision fixture server; never a dialogue simulator.

python -m scripts.participation_fixture --cases path.json --port 8772
Only POST /v1/participation/decide is implemented. Unmatched/ambiguous inputs
abstain. No sequence counter, member ordering, dialogue text or device state.
Use real Agent/Channel/reply models around this boundary for end-to-end runs.
"""

import argparse
import json
from pathlib import Path

from eidolon_sdk.biz.participation import (
    Contract,
    DecisionRequest,
    DecisionResult,
    Proposal,
    Snapshot,
    validate_proposal,
)
from fastapi import FastAPI
from pydantic import Field


class Case(Contract):
    user_text: str = Field(min_length=1)
    trigger_kind: str
    trigger_role: str | None = None
    trigger_contains: str | None = None
    completed_roles: tuple[str, ...] | None = None
    action: str
    speaker_role: str | None = None
    instruction: str = ""


class InputMatchedDecisions:
    def __init__(self, cases):
        self.cases = tuple(Case.model_validate(case) for case in cases)

    async def __call__(self, request: DecisionRequest) -> DecisionResult:
        names = {c.companion_id: c.display_name for c in request.candidates}
        current = []
        started = False
        for message in request.context.recent_messages:
            if message.message_id == request.user_request.message_id:
                started = True
            elif started and message.author_kind == "companion":
                current.append(names.get(message.author_id, ""))
        matches = [case for case in self.cases if (
            case.user_text == request.user_request.text
            and case.trigger_kind == request.trigger.author_kind
            and (case.trigger_role is None or
                 case.trigger_role == names.get(request.trigger.author_id))
            and (case.trigger_contains is None or case.trigger_contains in request.trigger.text)
            and (case.completed_roles is None or tuple(current) == case.completed_roles)
        )]
        proposal = None
        if len(matches) == 1:
            case = matches[0]
            speakers = tuple(key for key, name in names.items() if name == case.speaker_role)
            if case.action in {"wait", "finish"} or len(speakers) == 1:
                proposal = Proposal(action=case.action, participants=speakers,
                                    instruction=case.instruction)
        result = DecisionResult(
            **{key: getattr(request, key) for key in Snapshot.model_fields},
            status="decided" if proposal else "abstained", proposal=proposal,
            policy_version="input-fixture-v2", model_version="fixture-no-model",
        )
        validate_proposal(request, result)
        return result


def fixture_app(cases):
    app = FastAPI(title="Participation contract fixture (not a semantic model)")
    decide = InputMatchedDecisions(cases)

    @app.post("/v1/participation/decide", response_model=DecisionResult)
    async def decision(request: DecisionRequest):
        return await decide(request)

    return app


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8772)
    args = parser.parse_args()
    # Loopback only; remote access uses the deployment's authenticated tunnel.
    uvicorn.run(fixture_app(json.loads(args.cases.read_text())), host="127.0.0.1", port=args.port)
