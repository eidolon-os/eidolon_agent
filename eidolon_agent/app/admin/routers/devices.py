"""Admin: device list / revoke (revocation propagates via NATS KV)."""

from __future__ import annotations

from datetime import datetime, timezone

from eidolon_sdk.biz.runtime import (
    owner_revocation_keys,
    RuntimeTokenRevokedError,
    RuntimeUnauthenticatedError,
)
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

router = APIRouter()


class DeviceInfo(BaseModel):
    id: str
    owner_id: str
    companion_id: str | None = None
    name: str | None
    revoked: bool


class RotateDeviceTokenResponse(BaseModel):
    device_id: str
    device_token: str
    expires_at: datetime


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

    counts = await data_store.owner_data.delete_owner_data(owner_id)
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


@router.post("/devices/{device_id}/rotate", response_model=RotateDeviceTokenResponse)
async def rotate_device_token(device_id: str, request: Request) -> RotateDeviceTokenResponse:
    verifier = getattr(request.app.state, "pairing_verifier", None)
    pairing = getattr(request.app.state, "pairing", None)
    if verifier is None or pairing is None:
        raise HTTPException(status_code=503, detail="pairing is not configured")

    token = _bearer_token(request)
    try:
        verified = await verifier.verify(token)
    except RuntimeTokenRevokedError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc
    except RuntimeUnauthenticatedError as exc:
        raise HTTPException(status_code=401, detail=exc.message) from exc

    if verified.device_id != device_id:
        raise HTTPException(status_code=403, detail="token device_id does not match URL")

    issued = await pairing.rotate(verified)
    return RotateDeviceTokenResponse(
        device_id=issued.device_id,
        device_token=issued.token,
        expires_at=issued.expires_at,
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="missing bearer token")
    return token.strip()
