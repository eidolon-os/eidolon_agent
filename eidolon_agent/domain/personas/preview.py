"""Ephemeral draft conversation: only compile and generate; no runtime writes."""

import asyncio
import hashlib
import json

from eidolon_sdk.biz.persona import (
    PERSONA_REALIZER,
    PersonaAuthoringDraft,
    PersonaPreviewRequest,
    PersonaPreviewResponse,
    apply_persona_authoring,
    build_persona_genome_from_draft,
    persona_genome_hash,
)

from eidolon_agent.core.types.turn import TurnInput, TurnTrigger
from eidolon_agent.core.types.turn_context import TurnContext
from eidolon_agent.domain.context.compiler import ContextCompiler
from eidolon_agent.domain.history import HistoryManager
from eidolon_agent.domain.personas.realizer import PersonaRealizer
from eidolon_agent.domain.personas.types import StoredPersonaGenome


class _DraftPersona:
    def __init__(self, stored):
        self.stored = stored

    async def realize_context(self, **kwargs):
        return PersonaRealizer().realize(stored=self.stored, modality=kwargs["modality"])


async def preview_persona(
    draft: PersonaPreviewRequest, *, llm, base=None
) -> PersonaPreviewResponse:
    if draft.companion_id and base is None:
        raise ValueError("existing companion preview requires a standing genome")
    genome = (
        apply_persona_authoring(base.genome, draft.persona, base_genome_id=base.genome_id)
        if base is not None
        else build_persona_genome_from_draft(
            PersonaAuthoringDraft.for_companion(draft.persona, name=draft.name)
        )
    )
    digest = hashlib.sha256(
        json.dumps(
            draft.model_dump(mode="json", exclude={"text"}),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    stored = StoredPersonaGenome(
        owner_id="preview",
        companion_id="preview",
        genome_id=digest,
        genome_hash=persona_genome_hash(genome),
        realizer_version=PERSONA_REALIZER,
        version=1,
        genome=genome,
        conversation_preferences=draft.preferences,
    )
    compiler = ContextCompiler(
        personas_service=_DraftPersona(stored),
        instance_locator=lambda *_: ("preview", digest),
        history_manager=HistoryManager(),
    )
    turn = TurnInput(
        turn_id=digest,
        conversation_id="preview",
        session_id="preview",
        context=TurnContext(
            owner_id="preview",
            companion_id="preview",
            device_id=None,
            memory_realm_id="preview",
            genome_id=digest,
            trace_id=digest,
            request_id=digest,
            schema_version=genome.schema_version,
            genome_hash=stored.genome_hash,
            realizer_version=PERSONA_REALIZER,
        ),
        input_modality=draft.modality,
        trigger=TurnTrigger.USER_UTTERANCE,
        text=draft.text,
    )
    messages = await compiler.compile(turn)
    output = []
    finish = "unknown"
    async with asyncio.timeout(30):
        async for delta in llm.stream(messages, tools=[], temperature=0.0, request_id=digest):
            if delta.tool_call is not None:
                raise ValueError("preview cannot execute tools")
            if delta.text_delta:
                output.append(delta.text_delta)
            if delta.finish:
                finish = delta.finish.value
    if not output or finish not in {"stop", "length"}:
        raise ValueError("preview did not produce a completed reply")
    return PersonaPreviewResponse(
        draft_digest=digest,
        reply="".join(output),
        finish_reason=finish,
        truncated=finish == "length",
    )
