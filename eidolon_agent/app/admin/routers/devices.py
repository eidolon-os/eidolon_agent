"""Admin: device list / revoke (revocation propagates via NATS KV)."""

from __future__ import annotations

from datetime import datetime, timezone

from eidolon_sdk.biz.runtime import owner_revocation_keys
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

router = APIRouter()


class DeviceInfo(BaseModel):
    id: str
    owner_id: str
    companion_id: str | None = None
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


class RevokeOwnerSessionsResponse(BaseModel):
    owner_id: str
    revoked: bool


class DeleteOwnerDataResponse(BaseModel):
    owner_id: str
    deleted: bool
    counts: dict[str, int]
    revocation_keys_cleared: int = 0


@router.post(
    "/owners/{owner_id}/revoke-sessions",
    response_model=RevokeOwnerSessionsResponse,
    status_code=status.HTTP_200_OK,
)
async def revoke_owner_sessions(owner_id: str, request: Request) -> RevokeOwnerSessionsResponse:
    """Invalidate all active runtime tokens for an owner boundary."""
    kv = getattr(request.app.state, "revocation_kv", None)
    if kv is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "revocation_kv not configured on agent; the DEVICE_REVOCATIONS "
                "bucket failed to initialize at startup"
            ),
        )
    # Value can be anything truthy — verifier just checks key existence.
    # Store the timestamp for ops-side audit ("when was this revoked").
    timestamp = datetime.now(timezone.utc).isoformat()
    for key in owner_revocation_keys(owner_id):
        await kv.put(key, timestamp.encode("utf-8"))
    return RevokeOwnerSessionsResponse(owner_id=owner_id, revoked=True)


@router.delete(
    "/owners/{owner_id}/data",
    response_model=DeleteOwnerDataResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_owner_data(owner_id: str, request: Request) -> DeleteOwnerDataResponse:
    """Hard-delete Eidolon Data business rows for one owner.

    Admin calls this from its owner-delete cascade. Ownership and deletion order
    live in ``eidolon_data`` so agent does not carry unified-schema SQL.
    """
    data_store = getattr(request.app.state, "data_store", None)
    if data_store is None:
        raise HTTPException(
            status_code=503,
            detail="data_store not configured; cannot delete user data",
        )

    counts = await data_store.owner_data_ops.delete_owner_data(owner_id)
    cleared_revocations = await _clear_owner_revocations(request, owner_id)

    return DeleteOwnerDataResponse(
        owner_id=owner_id,
        deleted=True,
        counts=counts,
        revocation_keys_cleared=cleared_revocations,
    )


async def _clear_owner_revocations(request: Request, owner_id: str) -> int:
    cleared_revocations = 0
    kv = getattr(request.app.state, "revocation_kv", None)
    if kv is not None:
        for key in owner_revocation_keys(owner_id):
            delete_key = getattr(kv, "delete", None)
            if delete_key is not None and await kv.get(key) is not None:
                await delete_key(key)
                cleared_revocations += 1
    return cleared_revocations

