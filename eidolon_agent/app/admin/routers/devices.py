"""Admin: device list / revoke (revocation propagates via NATS KV)."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from pydantic import BaseModel

router = APIRouter()


class DeviceInfo(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    name: str | None
    revoked: bool


@router.get("/devices", response_model=list[DeviceInfo])
async def list_devices(request: Request):
    # Real impl reads from SQLite DeviceRepository via UoW. Skeleton returns [].
    return []


@router.delete("/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_device(device_id: str, request: Request):
    # Real impl: DeviceRepository.revoke + KV put on bucket DEVICE_REVOCATIONS.
    return None
