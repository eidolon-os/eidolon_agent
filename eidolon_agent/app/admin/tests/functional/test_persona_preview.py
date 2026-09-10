import httpx
from eidolon_sdk.biz.persona import (
    PERSONA_REALIZER,
    build_default_persona_genome,
    persona_genome_hash,
)
from fastapi import FastAPI

from eidolon_agent.app.admin.routers.persona_preview import router
from eidolon_agent.app.admin.tests.conftest import AUTHORITY_HEADERS
from eidolon_agent.core.types.llm import LLMDelta, LLMFinishReason
from eidolon_agent.domain.personas.types import StoredPersonaGenome


async def test_preview_is_authenticated_and_fences_owner_and_base():
    seen = []
    genome = build_default_persona_genome(name="Current")
    genome.character.tensions = ["HIDDEN_MARKER"]
    base = StoredPersonaGenome(
        owner_id="owner",
        companion_id="companion",
        genome_id="g1",
        genome_hash=persona_genome_hash(genome),
        realizer_version=PERSONA_REALIZER,
        version=1,
        genome=genome,
    )

    class Personas:
        async def get_preview_base(self, **kwargs):
            seen.append(kwargs)
            if kwargs["owner_id"] != "owner":
                raise ValueError("not owned")
            return base

    class Model:
        calls = 0

        async def stream(self, messages, **kwargs):
            self.calls += 1
            assert "HIDDEN_MARKER" in str(messages)
            assert kwargs["tools"] == []
            yield LLMDelta(text_delta="reply", finish=LLMFinishReason.STOP)

    model = Model()
    app = FastAPI()
    app.state.llm_router = model
    app.state.personas_service = Personas()
    app.include_router(router)
    draft = {
        "name": "Current",
        "persona": {},
        "text": "hi",
        "companion_id": "companion",
        "base_genome_id": "g1",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.post("/persona/preview?owner_id=owner", json=draft)).status_code == 401
        assert model.calls == 0 and seen == []
        assert (
            await client.post(
                "/persona/preview?owner_id=other", json=draft, headers=AUTHORITY_HEADERS
            )
        ).status_code == 404
        assert (
            await client.post(
                "/persona/preview?owner_id=owner",
                json={**draft, "base_genome_id": "stale"},
                headers=AUTHORITY_HEADERS,
            )
        ).status_code == 409
        reply = await client.post(
            "/persona/preview?owner_id=owner", json=draft, headers=AUTHORITY_HEADERS
        )
    assert reply.status_code == 200, reply.text
    assert reply.json()["reply"] == "reply"
    assert model.calls == 1
