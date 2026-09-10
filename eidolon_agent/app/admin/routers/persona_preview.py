"""Authenticated, stateless persona draft preview."""

from eidolon_sdk.biz.persona import PersonaPreviewRequest, PersonaPreviewResponse
from fastapi import APIRouter, HTTPException, Request

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES
from eidolon_agent.domain.personas.preview import preview_persona

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)


@router.post("/persona/preview", response_model=PersonaPreviewResponse)
async def preview(payload: PersonaPreviewRequest, request: Request, owner_id: str):
    llm = getattr(request.app.state, "llm_router", None)
    if llm is None:
        raise HTTPException(503, "preview model unavailable")
    base = None
    if payload.companion_id:
        personas = request.app.state.personas_service
        if personas is None:
            raise HTTPException(503, "persona authority unavailable")
        try:
            base = await personas.get_preview_base(
                owner_id=owner_id, companion_id=payload.companion_id
            )
        except Exception as exc:
            raise HTTPException(404, "companion unavailable") from exc
        if base.genome_id != payload.base_genome_id:
            raise HTTPException(409, "persona changed; reload before preview")
    try:
        return await preview_persona(payload, llm=llm, base=base)
    except TimeoutError as exc:
        raise HTTPException(504, "preview timed out") from exc
    except Exception as exc:
        raise HTTPException(502, "preview generation failed") from exc
