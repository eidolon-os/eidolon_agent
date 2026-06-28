"""Admin: pairing-code issuance + QR rendering."""

from __future__ import annotations

import io

import qrcode
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from eidolon_agent.core.types.identity import build_memory_space_id

router = APIRouter()


class IssuePairingCodeRequest(BaseModel):
    owner_id: str
    companion_id: str


class PairingMemoryReadiness(BaseModel):
    ready: bool
    owner_id: str
    companion_id: str
    memory_space_id: str
    memory_realm_id: str
    reason: str | None = None
    mcp_http_url: str | None = None


class IssuePairingCodeResponse(BaseModel):
    code: str
    expires_at: str
    pair_url: str
    memory: PairingMemoryReadiness | None = None


@router.post("/pairing/codes", response_model=IssuePairingCodeResponse)
async def issue_code(body: IssuePairingCodeRequest, request: Request):
    pairing = request.app.state.pairing
    identity = await _resolve_pairing_identity(
        owner_id=body.owner_id,
        companion_id=body.companion_id,
        request=request,
    )
    memory = await _ensure_memory_provisioned(identity=identity, request=request)
    rec = await pairing.issue_code(
        owner_id=identity.owner_id,
        companion_id=identity.companion_id,
        memory_realm_id=identity.memory_realm_id,
        genome_id=identity.genome_id,
        issued_by_actor="admin",
    )
    return IssuePairingCodeResponse(
        code=rec.code,
        expires_at=rec.expires_at.isoformat(),
        pair_url=f"eidolon://pair?code={rec.code}",
        memory=memory,
    )


@router.get("/pairing/codes/{code}.png")
async def code_qr(code: str, request: Request):
    img = qrcode.make(f"eidolon://pair?code={code}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


async def _ensure_memory_provisioned(
    *,
    identity: "_PairingIdentity",
    request: Request,
) -> PairingMemoryReadiness | None:
    """Require a live memory route before issuing a device pairing code.

    The agent refuses a token that would start an amnesiac long-term session.
    """
    routes = getattr(request.app.state, "memory_routes", None)
    if routes is None:
        return None

    memory_space_id = build_memory_space_id(memory_realm_id=identity.memory_realm_id)

    route, reason = await routes.route_status_for(memory_space_id)
    if route is None:
        refresher = getattr(request.app.state, "memory_discovery_refresher", None)
        if refresher is not None:
            await refresher.refresh_once()
            route, reason = await routes.route_status_for(memory_space_id)

    if route is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "memory_user_not_provisioned",
                "owner_id": identity.owner_id,
                "companion_id": identity.companion_id,
                "memory_space_id": memory_space_id,
                "reason": reason or "memory_route_unavailable",
                "action": (
                    "Create or repair this owner companion through eidolon_admin "
                    "before issuing a device pairing code."
                ),
            },
        )

    return PairingMemoryReadiness(
        ready=True,
        owner_id=identity.owner_id,
        companion_id=identity.companion_id,
        memory_space_id=memory_space_id,
        memory_realm_id=identity.memory_realm_id,
        reason=None,
        mcp_http_url=route.mcp_url,
    )


class _PairingIdentity(BaseModel):
    owner_id: str
    companion_id: str
    memory_realm_id: str
    genome_id: str


async def _resolve_pairing_identity(
    *,
    owner_id: str,
    companion_id: str,
    request: Request,
) -> _PairingIdentity:
    data_store = getattr(request.app.state, "data_store", None)
    if data_store is None:
        raise HTTPException(status_code=503, detail="data_store not configured")

    owner = await data_store.owners.get(owner_id)
    if owner is None or owner.status != "active":
        raise HTTPException(status_code=404, detail="owner not found or inactive")
    companion = await data_store.companions.get(companion_id)
    if companion is None or companion.owner_id != owner_id or companion.status != "active":
        raise HTTPException(status_code=404, detail="companion not found or inactive")
    if not companion.default_memory_realm_id:
        raise HTTPException(status_code=409, detail="companion has no default memory realm")
    if not companion.current_genome_id:
        raise HTTPException(status_code=409, detail="companion has no current genome")
    return _PairingIdentity(
        owner_id=owner_id,
        companion_id=companion_id,
        memory_realm_id=companion.default_memory_realm_id,
        genome_id=companion.current_genome_id,
    )
