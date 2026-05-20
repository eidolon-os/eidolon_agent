"""Health / readiness / liveness probes."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/livez")
def livez() -> dict:
    return {"status": "alive"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict:
    fn = request.app.state.readiness
    ready = fn() if callable(fn) else False
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready"}
    return {"status": "ready"}
