"""Owner-scoped Agent runtime revocation and deletion endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from eidolon_sdk.biz.runtime import owner_revocation_keys
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from eidolon_agent.app.admin.authority import AUTHORITY_DEPENDENCIES

router = APIRouter(dependencies=AUTHORITY_DEPENDENCIES)


class RevokeOwnerSessionsResponse(BaseModel):
    owner_id: str
    revoked: bool
    #: The instant everything before it stopped being valid. Returned because a
    #: caller relaying this to a person needs to be able to say *when*, and
    #: because the watermark is the whole mechanism: tokens issued after it work,
    #: which is what makes signing every device out something one can recover
    #: from (``eidolon_sdk.biz.runtime.owner_revocation_keys``).
    revoked_at: str


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
async def revoke_owner_sessions(
    owner_id: str,
    request: Request,
) -> RevokeOwnerSessionsResponse:
    """Stop every runtime token this Owner had until now.

    A watermark rather than a switch: the instant is stored and the verifier
    refuses tokens issued before it, so devices come back with a fresh token
    instead of being locked out for good.

    Token ``iat`` is a whole number of seconds and this instant carries
    microseconds, so a token minted in the same second as the revoke is refused
    too. That is the side to err on — the device retries — and it is why a caller
    should not treat one refusal right afterwards as the revoke having failed.
    """
    kv = getattr(request.app.state, "revocation_kv", None)
    if kv is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "revocation_kv not configured on agent; the DEVICE_REVOCATIONS "
                "bucket failed to initialize at startup"
            ),
        )
    timestamp = datetime.now(timezone.utc).isoformat()
    for key in owner_revocation_keys(owner_id):
        await kv.put(key, timestamp.encode("utf-8"))
    return RevokeOwnerSessionsResponse(
        owner_id=owner_id, revoked=True, revoked_at=timestamp
    )


@router.delete(
    "/owners/{owner_id}/data",
    response_model=DeleteOwnerDataResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_owner_data(
    owner_id: str,
    request: Request,
) -> DeleteOwnerDataResponse:
    """Hard-delete Agent-owned runtime rows for one Owner.

    System Data is a separate authority and is never mutated by this endpoint.
    """
    runtime_store = getattr(request.app.state, "runtime_store", None)
    if runtime_store is None:
        raise HTTPException(
            status_code=503,
            detail="runtime_store not configured; cannot delete Agent runtime data",
        )
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
