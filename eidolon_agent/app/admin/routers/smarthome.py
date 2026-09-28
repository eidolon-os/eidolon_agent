"""One transcript from a trusted Channel worker into the smart-home use case."""

from __future__ import annotations

import os

from eidolon_sdk.biz.smarthome import VoiceResult
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES
from eidolon_agent.domain.smarthome import SmartHomeCommand
from eidolon_agent.infra.interpretation.adapters.rules import RulesInterpreter
from eidolon_agent.infra.smarthome.channel import ChannelSmartHomeClient

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)


class SpokenCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: str = Field(min_length=1, max_length=128)
    device_ref: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    utterance: str = Field(min_length=1, max_length=512)


@router.post("/smarthome/command", response_model=VoiceResult)
async def spoken_command(body: SpokenCommand) -> VoiceResult:
    token = os.environ.get("EIDOLON_CHANNEL_PROVIDER_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(status_code=503, detail="smart home runtime credential missing")
    client = ChannelSmartHomeClient(
        base_url="http://127.0.0.1:8767", token=token
    )
    command = SmartHomeCommand(
        directory=client, executor=client, interpreter=RulesInterpreter()
    )
    return await command.handle(
        body.owner_id, body.device_ref, body.turn_id, body.utterance
    )
