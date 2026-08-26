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


class RuntimeCompanionResponse(BaseModel):
    """One Companion this Host currently has a live runtime for."""

    companion_id: str
    genome_id: str
    #: When this runtime was first resolved on this Host, and when anything last
    #: addressed it. Both are needed to say something true: the first alone
    #: cannot tell a Companion used a minute ago from one used at boot.
    started_at: str
    last_active_at: str


class OwnerRuntimeCompanionsResponse(BaseModel):
    """Which of this Owner's Companions are live here, newest use first.

    **Several at once is the normal case**, which is the whole reason this exists:
    a Host runs one set of services and keeps runtime context per Companion
    (plan §4.6), so "which one is active" was never a question with one answer.
    Consumers were inferring it from whether the Owner had a default — that is,
    from a routing fallback — and calling the result "running".

    What this is not: presence. Nothing on this Host tracks whether a body is
    connected, so a Companion listed here is one this Host can run, not one
    somebody can necessarily reach. A consumer that renders this as 在线 has
    replaced one guess with another.

    Absence from this list is meaningful *when this answer arrived*: no live
    runtime means no session is running. It says nothing at all when the Agent
    could not be asked, and a caller that cannot reach this route must report
    unknown rather than none.
    """

    owner_id: str
    companions: list[RuntimeCompanionResponse]


@router.get(
    "/owners/{owner_id}/runtime-companions",
    response_model=OwnerRuntimeCompanionsResponse,
    status_code=status.HTTP_200_OK,
)
async def list_owner_runtime_companions(
    owner_id: str,
    request: Request,
) -> OwnerRuntimeCompanionsResponse:
    """Read the runtime registry, scoped to one Owner.

    A read of live process state, not of a store: it is true for this Agent
    process and this run. That is the honest scope — the question "is my Eidolon
    running" is about right now, and a persisted answer would be a record of
    something that has since stopped.
    """

    registry = getattr(request.app.state, "agent_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=503,
            detail="agent registry not configured; runtime state cannot be read",
        )
    return OwnerRuntimeCompanionsResponse(
        owner_id=owner_id,
        companions=[
            RuntimeCompanionResponse(
                companion_id=inst.companion_id,
                genome_id=inst.genome_id,
                started_at=inst.created_at.isoformat(),
                last_active_at=(inst.last_active_at or inst.created_at).isoformat(),
            )
            for inst in registry.for_owner(owner_id)
        ],
    )


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
