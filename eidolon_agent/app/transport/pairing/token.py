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

**Drift sentinel** (Phase 33.A1): the contract is pinned by
``eidolon_admin/server/tests/test_runtime_token_contract.py``. That
test imports both copies and verifies a channel-signed token round-
trips through this verifier with every field intact. If you edit
this file and break the contract, that test fails — fix it before
merging.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
import uuid

import jwt

from eidolon_agent.core.errors import (
    TokenRevokedError,
    UnauthenticatedError,
)

_KV_SAFE_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _kv_safe_token(value: str) -> str:
    """Encode arbitrary identity strings into a NATS KV-safe path segment."""

    encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def device_revocation_keys(device_id: str) -> tuple[str, ...]:
    """Return revocation keys for a device id.

    ``device_id`` can be a MAC address (``1c:db:...``). NATS KV keys cannot
    contain ``:``, so new writes use an encoded namespace. For compatibility
    with existing simple-id entries, verifiers also read the legacy key when
    the raw id is already KV-safe.
    """

    keys = [f"revoked.device.{_kv_safe_token(device_id)}"]
    if _KV_SAFE_RE.fullmatch(device_id):
        keys.append(f"revoked.{device_id}")
    return tuple(keys)


def user_revocation_keys(user_id: str) -> tuple[str, ...]:
    """Return revocation keys for all sessions belonging to a user."""

    keys = [f"revoked.user.v2.{_kv_safe_token(user_id)}"]
    if _KV_SAFE_RE.fullmatch(user_id):
        keys.append(f"revoked.user.{user_id}")
    return tuple(keys)


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
        user_id = payload.get("user_id") or ""

        # Phase 33.B1: revocation is checked at TWO scopes — a specific
        # device (legacy, used by pairing rotate / device admin) AND a
        # whole user (admin disables the user account, wants all in-
        # flight sessions cut off). Either match → reject.
        #
        # Key conventions:
        #   - ``revoked.device.<base64url(device_id)>`` — single device
        #   - ``revoked.user.v2.<base64url(user_id)>`` — every device
        #     belonging to the user; channel's per-session JWTs have
        #     ``device_id="web-xxx"`` so user-level is the only effective
        #     revocation for them
        #
        # Verifier also checks legacy keys for KV-safe ids:
        #   - ``revoked.<device_id>``
        #   - ``revoked.user.<user_id>``
        if self._kv is not None:
            for key in device_revocation_keys(device_id):
                if await self._kv.get(key):
                    raise TokenRevokedError(f"device revoked: {device_id}")
            if user_id:
                for key in user_revocation_keys(user_id):
                    if await self._kv.get(key):
                        raise TokenRevokedError(
                            f"all sessions revoked for user: {user_id}"
                        )

        return VerifiedDevice(
            device_id=device_id,
            tenant_id=payload.get("tenant_id", ""),
            user_id=user_id,
            default_template_id=payload.get("template_id"),
            scopes=tuple(payload.get("scopes") or ()),
            exp=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
        )
