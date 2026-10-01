"""One transcript from a trusted Channel worker into the smart-home use case."""

from __future__ import annotations

from eidolon_sdk.biz.smarthome import HomeCommandRequest, HomeSessionScope, VoiceResult
from fastapi import APIRouter, HTTPException, Request

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES
from eidolon_agent.app.smarthome.sessions import HomeSessionUnavailable
from eidolon_agent.core.errors import (
    DependencyError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)


@router.post("/smarthome/command", response_model=VoiceResult)
async def spoken_command(body: HomeCommandRequest, request: Request) -> VoiceResult:
    application = request.app.state.smart_home_application
    if application is None:
        raise HTTPException(status_code=503, detail="smart home application is unavailable")
    try:
        return await application.handle(
            HomeSessionScope.model_validate(body.model_dump(exclude={"turn_id", "utterance"})),
            body.turn_id, body.utterance,
        )
    except HomeSessionUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (NotFoundError, PermissionDeniedError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DependencyError as exc:
        raise HTTPException(status_code=503, detail="runtime authority is unavailable") from exc


@router.post("/smarthome/session/end", status_code=204)
async def end_home_session(body: HomeSessionScope, request: Request) -> None:
    application = request.app.state.smart_home_application
    if application is not None:
        try:
            application.end_session(body)
        except HomeSessionUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
