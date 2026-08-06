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
    revocation_keys_written: int = 0


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
    """Hard-delete Agent-owned runtime rows for one owner.

    System Data is a separate authority and is never mutated by this endpoint.
    """
    runtime_store = getattr(request.app.state, "runtime_store", None)
    if runtime_store is None:
        raise HTTPException(
            status_code=503,
            detail="runtime_store not configured; cannot delete Agent runtime data",
        )

    # Revoke first: if process/database deletion fails, stale credentials stay
    # denied and the orchestrator can safely retry the idempotent cleanup.
    written_revocations = await _write_owner_revocations(request, owner_id)
    counts = await runtime_store.delete_owner_runtime(owner_id)

    return DeleteOwnerDataResponse(
        owner_id=owner_id,
        deleted=True,
        counts=counts,
        revocation_keys_written=written_revocations,
    )


@router.delete(
    "/owners/{owner_id}/companions/{companion_id}/data",
    response_model=DeleteOwnerDataResponse,
)
async def delete_companion_data(
    owner_id: str,
    companion_id: str,
    request: Request,
) -> DeleteOwnerDataResponse:
    runtime_store = getattr(request.app.state, "runtime_store", None)
    if runtime_store is None:
        raise HTTPException(503, "runtime_store not configured")
    counts = await runtime_store.delete_companion_runtime(owner_id, companion_id)
    return DeleteOwnerDataResponse(
        owner_id=owner_id,
        deleted=True,
        counts=counts,
    )


async def _write_owner_revocations(request: Request, owner_id: str) -> int:
    written = 0
    kv = getattr(request.app.state, "revocation_kv", None)
    if kv is not None:
        timestamp = datetime.now(timezone.utc).isoformat().encode("utf-8")
        for key in owner_revocation_keys(owner_id):
            await kv.put(key, timestamp)
            written += 1
    return written
