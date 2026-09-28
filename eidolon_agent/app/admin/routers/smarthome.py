"""One transcript from a trusted Channel worker into the smart-home use case."""

from __future__ import annotations

from eidolon_sdk.biz.smarthome import VoiceResult
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES
from eidolon_agent.app.smarthome.sessions import HomeSessionUnavailable

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)


class SpokenCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=128)
    device_ref: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    utterance: str = Field(min_length=1, max_length=512)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/smarthome/command", response_model=VoiceResult)
async def spoken_command(body: SpokenCommand, request: Request) -> VoiceResult:
    application = request.app.state.smart_home_application
    if application is None:
        raise HTTPException(status_code=503, detail="smart home application is unavailable")
    try:
        return await application.handle(
            body.owner_id, body.device_ref, body.turn_id, body.utterance, session_id=body.session_id,
        )
    except HomeSessionUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class EndHomeSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=128)
    device_ref: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)


@router.post("/smarthome/session/end", status_code=204)
async def end_home_session(body: EndHomeSession, request: Request) -> None:
    application = request.app.state.smart_home_application
    if application is not None:
        application.end_session(body.owner_id, body.device_ref, body.session_id)
