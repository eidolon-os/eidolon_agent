"""Explicit demo policy, never presented as natural-language adjudication."""

from eidolon_sdk.biz.participation import DecisionRequest, DecisionResult, Proposal, Snapshot


class MockDecision:
    def __init__(self, order: tuple[str, ...]):
        if not order or len(set(order)) != len(order):
            raise ValueError("explicit unique demo order required")
        self.order = order

    async def __call__(self, request: DecisionRequest) -> DecisionResult:
        available = {c.companion_id for c in request.candidates}
        order = tuple(c for c in self.order if c in available)
        if request.trigger.author_kind == "companion" and order:
            previous = request.trigger.author_id
            start = (self.order.index(previous) + 1) if previous in self.order else 0
            rotated = self.order[start:] + self.order[:start]
            order = tuple(c for c in rotated if c in available)[:1]
        order = order[: request.constraints.max_next_speakers]
        proposal = (
            Proposal(action="respond", participants=order) if order else Proposal(action="wait")
        )
        return DecisionResult(
            **{key: getattr(request, key) for key in Snapshot.model_fields},
            status="decided",
            proposal=proposal,
            policy_version="explicit-demo-order-v1",
            model_version="mock-no-model",
        )
