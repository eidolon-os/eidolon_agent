"""Admin: pairing-code issuance + QR rendering."""

from __future__ import annotations

import io

import qrcode
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()


class IssuePairingCodeRequest(BaseModel):
    tenant_id: str
    user_id: str
    default_template_id: str | None = None


class PairingMemoryReadiness(BaseModel):
    ready: bool
    user_id: str
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
    memory = await _ensure_memory_provisioned(body.user_id, request)
    rec = await pairing.issue_code(
        tenant_id=body.tenant_id,
        user_id=body.user_id,
        default_template_id=body.default_template_id,
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
    user_id: str, request: Request
) -> PairingMemoryReadiness | None:
    """Require a live memory route before issuing a device pairing code.

    The full provisioning authority lives in eidolon_admin/eidolon_memory:
    admin creates users through memory's ``/api/admin/users`` surface. The
    agent should not create users or fall back to ``default`` here; it only
    refuses a token that would otherwise start an amnesiac long-term session.
    """
    routes = getattr(request.app.state, "memory_routes", None)
    if routes is None:
        return None

    route, reason = await routes.route_status_for(user_id)
    if route is None:
        refresher = getattr(request.app.state, "memory_discovery_refresher", None)
        if refresher is not None:
            await refresher.refresh_once()
            route, reason = await routes.route_status_for(user_id)

    if route is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "memory_user_not_provisioned",
                "user_id": user_id,
                "reason": reason or "memory_route_unavailable",
                "action": (
                    "Create or repair this user through eidolon_admin /api/users "
                    "before issuing a device pairing code."
                ),
            },
        )

    return PairingMemoryReadiness(
        ready=True,
        user_id=user_id,
        reason=None,
        mcp_http_url=route.mcp_url,
    )
