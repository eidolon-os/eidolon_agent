"""Admin: pairing-code issuance + QR rendering."""

from __future__ import annotations

import io

import qrcode
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel

router = APIRouter()


class IssuePairingCodeRequest(BaseModel):
    tenant_id: str
    user_id: str
    default_template_id: str | None = None


class IssuePairingCodeResponse(BaseModel):
    code: str
    expires_at: str
    pair_url: str


@router.post("/pairing/codes", response_model=IssuePairingCodeResponse)
async def issue_code(body: IssuePairingCodeRequest, request: Request):
    pairing = request.app.state.pairing
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
    )


@router.get("/pairing/codes/{code}.png")
async def code_qr(code: str, request: Request):
    img = qrcode.make(f"eidolon://pair?code={code}")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")
