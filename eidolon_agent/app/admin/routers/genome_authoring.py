"""Admin: companion-first genome authoring (assemble authored content + persist).

Owners author a companion's genome via eidolon_admin, which proxies here so the
persona logic (schema, defaults, validation, prompt render) stays in the agent.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

router = APIRouter()


class AuthorGenomeRequest(BaseModel):
    name: str
    archetype: str = "companion"
    description: str = ""
    pronouns: str = "她"
    values: list[str] = []
    taboos: list[str] = []
    unbreakable_rules: list[str] = []
    style: list[str] = []
    example_dialogs: list[str] = []
    goals: list[str] = []
    pinned_facts: list[str] = []
    relationship_stage: str = ""
    knobs: dict[str, float] | None = None


class AuthoredGenomeResponse(BaseModel):
    owner_id: str
    companion_id: str
    version: int
    genome_id: str
    name: str
    archetype: str


@router.post(
    "/owners/{owner_id}/companions/{companion_id}/genome/author",
    response_model=AuthoredGenomeResponse,
)
async def author_companion_genome(
    owner_id: str,
    companion_id: str,
    payload: AuthorGenomeRequest,
    request: Request,
) -> AuthoredGenomeResponse:
    service = getattr(request.app.state, "personas_service", None)
    if service is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "personas_service unavailable")
    try:
        persona = await service.author_genome(
            owner_id=owner_id,
            companion_id=companion_id,
            name=payload.name,
            archetype=payload.archetype,
            description=payload.description,
            pronouns=payload.pronouns,
            values=tuple(payload.values),
            taboos=tuple(payload.taboos),
            unbreakable_rules=tuple(payload.unbreakable_rules),
            style=tuple(payload.style),
            example_dialogs=tuple(payload.example_dialogs),
            goals=tuple(payload.goals),
            pinned_facts=tuple(payload.pinned_facts),
            relationship_stage=payload.relationship_stage,
            knobs=payload.knobs,
        )
    except Exception as exc:  # noqa: BLE001 - surface assembly/validation errors as 400
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return AuthoredGenomeResponse(
        owner_id=persona.owner_id,
        companion_id=persona.companion_id,
        version=persona.version,
        genome_id=f"genome-{persona.companion_id}-{persona.version}",
        name=persona.metadata.name,
        archetype=persona.metadata.archetype,
    )
