"""Validate the model's terminal response against facts owned by this turn.

Uses the existing provider tool schema mechanism: one terminal response function,
not another model call or an emotion classifier. It never enters ToolDispatcher.
"""

from copy import deepcopy
from dataclasses import dataclass, replace

from eidolon_sdk.biz.presentation import AssistantResponseCandidate, OutputSelection, ResponseIntent
from pydantic import ValidationError

from eidolon_agent.core.types.tool import ToolResult, ToolSchema


class InvalidPresentationError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedResponse:
    candidate: AssistantResponseCandidate
    intent: ResponseIntent
    presentation_error: str | None = None


def resolve_response(
    arguments: dict,
    *,
    turn_id: str,
    session_id: str,
    outcomes: dict[str, ToolResult],
    outputs: OutputSelection | None = None,
) -> ResolvedResponse:
    """Isolate optional expression failure from a valid public answer.

    The strict validator remains the authority for expression facts. A rejected
    expression is omitted, never relabelled as a successful action. Language
    still passes its own schema and the turn's normal output guardrail.
    """
    context = dict(turn_id=turn_id, session_id=session_id, outcomes=outcomes, outputs=outputs)
    try:
        candidate, intent = validate_response(arguments, **context)
        return ResolvedResponse(candidate, intent)
    except InvalidPresentationError as error:
        candidate, intent = validate_response(
            {**arguments, "presentation": {"intent": "none"}}, **context
        )
        # Nothing independently deliverable remains. Do not turn a malformed
        # response or a silent expression-only failure into a successful answer.
        if not candidate.public_text or not candidate.public_text.strip():
            raise error
        return ResolvedResponse(candidate, intent, str(error))


RESPONSE_TOOL = "eidolon_respond"
RESPONSE_SCHEMA = ToolSchema(
    name=RESPONSE_TOOL,
    description=(
        "Finish this turn with a structured response and expressive intent. "
        "Call this alone after all needed tools finish. Do not output raw text. "
        "acknowledge means heard, not done or memorized. confirm/celebrate require "
        "outcome_ref pointing to a successful, completed tool call in this turn. "
        "Use public_text for the answer when language output is selected. "
        "Expression is supplementary and never substitutes for a verbal answer. "
        "Do not claim memory was saved merely because a background write was queued."
    ),
    json_schema=AssistantResponseCandidate.model_json_schema(),
)


def response_schema(outputs: OutputSelection | None) -> ToolSchema:
    """Specialize the existing final-response schema for the selected outputs.

    A speech/text conversation needs a public answer. A face profile only says
    how to render expression; it must not silently turn voice into silent mode.
    Legacy callers without selection retain the optional public_text contract.
    """
    if outputs is None:
        return RESPONSE_SCHEMA
    schema = deepcopy(RESPONSE_SCHEMA.json_schema)
    language = outputs.speech or outputs.dialogue_text
    if not outputs.expression and not outputs.motion:
        # No expressive output was selected: do not ask the model to generate
        # gesture/stance/intensity fields that would only be discarded.
        schema["properties"].pop("presentation", None)
        schema.pop("$defs", None)
        schema["required"] = [key for key in schema.get("required", []) if key != "presentation"]
        if not language:
            schema["properties"].pop("public_text", None)
            schema["properties"].pop("schema_version", None)
            schema["required"] = []
            return replace(RESPONSE_SCHEMA, json_schema=schema,
                description="Finish this turn without a public response. Call this alone with {} after all needed tools finish. Do not generate reply text or expressive intent.")
    schema["properties"]["public_text"] = (
        {
            "anyOf": [{"type": "string", "minLength": 1, "maxLength": 8192}, {"type": "null"}],
            "description": "The public answer to speak or display. Null only for intentional intent=none suppression; expression otherwise supplements the answer.",
        }
        if language
        else {"type": "null", "description": "No language output selected."}
    )
    schema["required"] = list(dict.fromkeys([*schema.get("required", []), "public_text",
        *(["presentation"] if outputs.expression or outputs.motion else [])]))
    return replace(RESPONSE_SCHEMA, json_schema=schema)


def validate_response(
    arguments: dict,
    *,
    turn_id: str,
    session_id: str,
    outcomes: dict[str, ToolResult],
    outputs: OutputSelection | None = None,
) -> tuple[AssistantResponseCandidate, ResponseIntent]:
    try:
        candidate = AssistantResponseCandidate.model_validate(arguments)
    except ValidationError as exc:
        raise InvalidPresentationError("INVALID_RESPONSE_CANDIDATE") from exc
    if outputs is not None:
        if outputs.speech or outputs.dialogue_text:
            if candidate.presentation.intent != "none" and (
                not candidate.public_text or not candidate.public_text.strip()
            ):
                raise InvalidPresentationError("PUBLIC_ANSWER_REQUIRED")
        elif candidate.public_text is not None:
            # The output boundary also enforces selection. Do not place a
            # shadow, unspoken answer into history in a strict silent session.
            candidate = candidate.model_copy(update={"public_text": None})
    if outputs is not None and not (outputs.expression or outputs.motion):
        from eidolon_sdk.biz.presentation import PresentationCandidate
        candidate = candidate.model_copy(update={"presentation": PresentationCandidate(intent="none")})
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
