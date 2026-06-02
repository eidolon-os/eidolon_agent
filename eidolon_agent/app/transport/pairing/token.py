"""JWT-based device tokens.

Issued by :func:`sign_device_token`; verified by :class:`PairingTokenVerifier`.
The verifier consults the device revocation list (NATS KV bucket
``DEVICE_REVOCATIONS``) on every call so admin-side revocations propagate
instantly without re-issuing tokens.

**Cross-project schema coupling** (Phase 32.B): a sibling implementation
of ``sign_device_token`` lives in ``eidolon_channel`` at
``eidolon/livekit/agent/runtime/token_signer.py``. It uses the SAME
payload schema (``device_id`` / ``tenant_id`` / ``user_id`` /
``template_id`` / ``scopes`` / ``jti`` / ``exp`` / ``iat``) and the
SAME HMAC secret (``PAIRING_JWT_SECRET`` env or
``~/eidolon/run/jwt-secret`` file). If you change ANY field name or
algorithm here, you MUST update the channel copy + run tests on both
sides — otherwise channel-signed tokens will fail verification here
and all web/esp32 conversations break. The duplication exists because
channel doesn't want a hard pkg import on eidolon_agent (separate
venv, separate deploy unit) — a shared ``eidolon-runtime-tokens`` pkg
is the long-term fix but not warranted for ~80 lines of code today.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

import jwt

from eidolon_agent.core.errors import (
    TokenRevokedError,
    UnauthenticatedError,
)


@dataclass(frozen=True, slots=True)
class VerifiedDevice:
    device_id: str
    tenant_id: str
    user_id: str
    default_template_id: str | None
    scopes: tuple[str, ...]
    exp: datetime


def sign_device_token(
    *,
    secret: str,
    algorithm: str = "HS256",
    device_id: str,
    tenant_id: str,
    user_id: str,
    default_template_id: str | None,
    scopes: list[str],
    ttl_days: int = 30,
) -> tuple[str, datetime]:
    exp = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    payload = {
        "device_id": device_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "template_id": default_template_id,
        "scopes": scopes,
        "jti": uuid.uuid4().hex,
        "exp": int(exp.timestamp()),
        "iat": int(datetime.now(timezone.utc).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm), exp


class PairingTokenVerifier:
    def __init__(
        self,
        *,
        secret: str,
        algorithm: str = "HS256",
        revocation_kv=None,  # KVStore, optional
    ) -> None:
        self._secret = secret
        self._alg = algorithm
        self._kv = revocation_kv

    async def verify(self, token: str) -> VerifiedDevice:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._alg])
        except jwt.PyJWTError as exc:
            raise UnauthenticatedError(f"invalid token: {exc}") from exc
        device_id = payload.get("device_id")
        if not device_id:
            raise UnauthenticatedError("token missing device_id")
        if self._kv is not None and await self._kv.get(f"revoked.{device_id}"):
            raise TokenRevokedError(f"device revoked: {device_id}")
        return VerifiedDevice(
            device_id=device_id,
            tenant_id=payload.get("tenant_id", ""),
            user_id=payload.get("user_id", ""),
            default_template_id=payload.get("template_id"),
            scopes=tuple(payload.get("scopes") or ()),
            exp=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
