"""Validate the model's terminal response against facts owned by this turn.

Uses the existing provider tool schema mechanism: one terminal response function,
not another model call or an emotion classifier. It never enters ToolDispatcher.
"""

from eidolon_sdk.biz.presentation import AssistantResponseCandidate, ResponseIntent
from pydantic import ValidationError

from eidolon_agent.core.types.tool import ToolResult, ToolSchema


class InvalidPresentationError(ValueError):
    pass


RESPONSE_TOOL = "eidolon_respond"
RESPONSE_SCHEMA = ToolSchema(
    name=RESPONSE_TOOL,
    description=(
        "Finish this turn with a structured response and expressive intent. "
        "Call this alone after all needed tools finish. Do not output raw text. "
        "acknowledge means heard, not done or memorized. confirm/celebrate require "
        "outcome_ref pointing to a successful, completed tool call in this turn. "
        "Use clarify when an expression cannot communicate the answer. "
        "Do not claim memory was saved merely because a background write was queued."
    ),
    json_schema=AssistantResponseCandidate.model_json_schema(),
)


def validate_response(
    arguments: dict, *, turn_id: str, session_id: str, outcomes: dict[str, ToolResult]
) -> tuple[AssistantResponseCandidate, ResponseIntent]:
    try:
        candidate = AssistantResponseCandidate.model_validate(arguments)
    except ValidationError as exc:
        raise InvalidPresentationError("INVALID_RESPONSE_CANDIDATE") from exc
    presentation = candidate.presentation
    outcome = outcomes.get(presentation.outcome_ref or "")
    # Tool success alone may only mean a request was accepted. Completion is an
    # explicit dispatcher/tool result fact, never inferred from LLM prose.
    completed = bool(
        outcome and outcome.ok and outcome.metadata.get("outcome_state") == "completed"
    )
    if presentation.outcome_ref is not None and not completed:
        raise InvalidPresentationError("OUTCOME_NOT_COMPLETED_IN_THIS_TURN")
    if presentation.intent in {"confirm", "celebrate"} and not completed:
        raise InvalidPresentationError("SUCCESS_INTENT_REQUIRES_COMPLETED_OUTCOME")
    intent = ResponseIntent(
        **presentation.model_dump(),
        response_id=f"response:{turn_id}",
        turn_id=turn_id,
        session_id=session_id,
    )
    return candidate, intent
