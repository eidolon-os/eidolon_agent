"""Generate pairing codes and exchange them for device tokens."""

from __future__ import annotations

import asyncio
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from eidolon_sdk.biz.runtime import RuntimeIdentity, sign_device_token

from eidolon_agent.core.errors import NotFoundError, UnauthenticatedError

_ALPHABET = string.ascii_uppercase + string.digits


@dataclass(frozen=True, slots=True)
class PairingCode:
    code: str
    owner_id: str
    companion_id: str
    memory_realm_id: str
    genome_id: str
    issued_at: datetime
    expires_at: datetime
    issued_by_actor: str


@dataclass(frozen=True, slots=True)
class IssuedToken:
    device_id: str
    token: str
    expires_at: datetime
    owner_id: str
    companion_id: str
    memory_realm_id: str
    genome_id: str


class PairingCoordinator:
    def __init__(
        self,
        *,
        jwt_secret: str,
        jwt_algorithm: str = "HS256",
        code_ttl_s: int = 600,
        code_length: int = 8,
        token_ttl_days: int = 30,
    ) -> None:
        self._secret = jwt_secret
        self._alg = jwt_algorithm
        self._code_ttl_s = code_ttl_s
        self._code_length = code_length
        self._token_ttl_days = token_ttl_days
        self._codes: dict[str, PairingCode] = {}
        self._lock = asyncio.Lock()

    async def issue_code(
        self,
        *,
        owner_id: str,
        companion_id: str,
        memory_realm_id: str,
        genome_id: str,
        issued_by_actor: str,
    ) -> PairingCode:
        code = "".join(secrets.choice(_ALPHABET) for _ in range(self._code_length))
        now = datetime.now(timezone.utc)
        record = PairingCode(
            code=code,
            owner_id=owner_id,
            companion_id=companion_id,
            memory_realm_id=memory_realm_id,
            genome_id=genome_id,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._code_ttl_s),
            issued_by_actor=issued_by_actor,
        )
        async with self._lock:
            self._codes[code] = record
        return record

    async def exchange(self, *, code: str, device_id: str | None) -> IssuedToken:
        async with self._lock:
            rec = self._codes.pop(code, None)
        if rec is None:
            raise NotFoundError("pairing code not found")
        if datetime.now(timezone.utc) >= rec.expires_at:
            raise UnauthenticatedError("pairing code expired")
        device_id = device_id or f"dev_{uuid.uuid4().hex[:12]}"
        token, exp = sign_device_token(
            secret=self._secret,
            algorithm=self._alg,
            device_id=device_id,
            owner_id=rec.owner_id,
            companion_id=rec.companion_id,
            memory_realm_id=rec.memory_realm_id,
            genome_id=rec.genome_id,
            scopes=["device"],
            ttl_seconds=self._token_ttl_days * 24 * 3600,
        )
        return IssuedToken(
            device_id=device_id,
            token=token,
            expires_at=exp,
            owner_id=rec.owner_id,
            companion_id=rec.companion_id,
            memory_realm_id=rec.memory_realm_id,
            genome_id=rec.genome_id,
        )

    async def rotate(self, identity: RuntimeIdentity) -> IssuedToken:
        token, exp = sign_device_token(
            secret=self._secret,
            algorithm=self._alg,
            device_id=identity.device_id,
            owner_id=identity.owner_id,
            companion_id=identity.companion_id,
            memory_realm_id=identity.memory_realm_id,
            genome_id=identity.genome_id,
            scopes=list(identity.scopes) or ["device"],
            ttl_seconds=self._token_ttl_days * 24 * 3600,
        )
        return IssuedToken(
            device_id=identity.device_id,
            token=token,
            expires_at=exp,
            owner_id=identity.owner_id,
            companion_id=identity.companion_id,
            memory_realm_id=identity.memory_realm_id,
            genome_id=identity.genome_id,
        )
