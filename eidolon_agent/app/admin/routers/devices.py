"""Admin: device list / revoke (revocation propagates via NATS KV)."""

from __future__ import annotations

from datetime import datetime, timezone

from eidolon_sdk.runtime import (
    RuntimeTokenRevokedError,
    RuntimeUnauthenticatedError,
    user_revocation_keys,
)
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, select

from eidolon_agent.infra.persistence.models import (
    ChatMessageRow,
    ConversationRow,
    DeviceRow,
    EvolutionHistoryRow,
    LongTaskRow,
    PersonaEvolutionProposalRow,
    PersonaInstanceRow,
    PersonaObservationRow,
    TurnRow,
)

router = APIRouter()


class DeviceInfo(BaseModel):
    id: str
    tenant_id: str
    user_id: str
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


class RevokeUserSessionsResponse(BaseModel):
    user_id: str
    revoked: bool


class DeleteUserDataResponse(BaseModel):
    user_id: str
    deleted: bool
    counts: dict[str, int]
    revocation_keys_cleared: int = 0


@router.post(
    "/users/{user_id}/revoke-sessions",
    response_model=RevokeUserSessionsResponse,
    status_code=status.HTTP_200_OK,
)
async def revoke_user_sessions(user_id: str, request: Request) -> RevokeUserSessionsResponse:
    """Phase 33.B1: invalidate ALL active runtime tokens for a user.

    Writes ``revoked.user.<user_id>`` to the ``DEVICE_REVOCATIONS`` KV
    bucket. ``PairingTokenVerifier.verify`` checks this key on every
    gRPC call — so the next chat() turn for any session of this user
    fails with ``RuntimeTokenRevokedError`` → LK session aborts → web client
    sees ``transport.sidecar_unavailable`` and must re-connect (which
    will fail at hub /api/config 404 if admin also disabled the user).

    Use cases:
      - Operator disables a user account in admin UI; want active calls
        cut off immediately, not at next token expiry (24h).
      - Suspected token leak; revoke before rotating the secret.

    The bucket entry has no TTL — operator must explicitly delete it
    to un-revoke the user. (TODO: add a DELETE endpoint for that.)
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
    # Value can be anything truthy — verifier just checks key existence.
    # Store the timestamp for ops-side audit ("when was this revoked").
    timestamp = datetime.now(timezone.utc).isoformat()
    for key in user_revocation_keys(user_id):
        await kv.put(key, timestamp.encode("utf-8"))
    return RevokeUserSessionsResponse(user_id=user_id, revoked=True)


@router.delete(
    "/users/{user_id}/data",
    response_model=DeleteUserDataResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_user_data(user_id: str, request: Request) -> DeleteUserDataResponse:
    """Hard-delete agent-owned persistent data for one user.

    Admin calls this from its user-delete cascade after it has removed
    registered persona agents. This second pass removes data that is keyed
    directly by ``user_id`` or may outlive an individual persona instance:
    conversation history, long tasks, runtime device tokens, persona
    observations/proposals, evolution history, and any orphan persona
    instances not present in admin's agent registry.
    """
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=503,
            detail="session_factory not configured; cannot delete user data",
        )

    async with session_factory() as session, session.begin():
        instance_ids = list(
            (
                await session.execute(
                    select(PersonaInstanceRow.id).where(
                        PersonaInstanceRow.user_id == user_id
                    )
                )
            ).scalars()
        )
        conversation_ids = list(
            (
                await session.execute(
                    select(ConversationRow.id).where(ConversationRow.user_id == user_id)
                )
            ).scalars()
        )
        turn_ids: list[str] = []
        if conversation_ids:
            turn_ids = list(
                (
                    await session.execute(
                        select(TurnRow.id).where(
                            TurnRow.conversation_id.in_(conversation_ids)
                        )
                    )
                ).scalars()
            )

        counts: dict[str, int] = {}
        if turn_ids:
            counts["chat_messages"] = _rowcount(
                await session.execute(
                    delete(ChatMessageRow).where(ChatMessageRow.turn_id.in_(turn_ids))
                )
            )
        else:
            counts["chat_messages"] = 0
        if conversation_ids:
            counts["turns"] = _rowcount(
                await session.execute(
                    delete(TurnRow).where(TurnRow.conversation_id.in_(conversation_ids))
                )
            )
        else:
            counts["turns"] = 0
        counts["conversations"] = _rowcount(
            await session.execute(
                delete(ConversationRow).where(ConversationRow.user_id == user_id)
            )
        )
        counts["long_tasks"] = _rowcount(
            await session.execute(delete(LongTaskRow).where(LongTaskRow.user_id == user_id))
        )
        counts["runtime_devices"] = _rowcount(
            await session.execute(delete(DeviceRow).where(DeviceRow.user_id == user_id))
        )
        counts["persona_observations"] = _rowcount(
            await session.execute(
                delete(PersonaObservationRow).where(PersonaObservationRow.user_id == user_id)
            )
        )
        counts["persona_evolution_proposals"] = _rowcount(
            await session.execute(
                delete(PersonaEvolutionProposalRow).where(
                    PersonaEvolutionProposalRow.user_id == user_id
                )
            )
        )
        if instance_ids:
            counts["evolution_history"] = _rowcount(
                await session.execute(
                    delete(EvolutionHistoryRow).where(
                        EvolutionHistoryRow.instance_id.in_(instance_ids)
                    )
                )
            )
        else:
            counts["evolution_history"] = 0
        counts["persona_instances"] = _rowcount(
            await session.execute(
                delete(PersonaInstanceRow).where(PersonaInstanceRow.user_id == user_id)
            )
        )

    cleared_revocations = 0
    kv = getattr(request.app.state, "revocation_kv", None)
    if kv is not None:
        for key in user_revocation_keys(user_id):
            delete_key = getattr(kv, "delete", None)
            if delete_key is not None and await kv.get(key) is not None:
                await delete_key(key)
                cleared_revocations += 1

    return DeleteUserDataResponse(
        user_id=user_id,
        deleted=True,
        counts=counts,
        revocation_keys_cleared=cleared_revocations,
    )


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


def _rowcount(result) -> int:
    return int(result.rowcount or 0)
