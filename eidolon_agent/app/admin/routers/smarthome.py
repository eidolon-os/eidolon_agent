"""One transcript from a trusted Channel worker into the smart-home use case."""

from __future__ import annotations

from eidolon_sdk.biz.smarthome import VoiceResult
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)


class SpokenCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=128)
    device_ref: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    utterance: str = Field(min_length=1, max_length=512)


@router.post("/smarthome/command", response_model=VoiceResult)
async def spoken_command(body: SpokenCommand, request: Request) -> VoiceResult:
    application = request.app.state.smart_home_application
    if application is None:
        raise HTTPException(status_code=503, detail="smart home application is unavailable")
    return await application.handle(body.owner_id, body.device_ref, body.turn_id, body.utterance)
