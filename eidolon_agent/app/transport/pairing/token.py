"""Agent compatibility wrapper for SDK runtime device tokens."""

from __future__ import annotations

from eidolon_sdk import runtime as sdk_runtime

from eidolon_agent.core.errors import (
    TokenRevokedError,
    UnauthenticatedError,
)

VerifiedDevice = sdk_runtime.VerifiedDevice
device_revocation_keys = sdk_runtime.device_revocation_keys
sign_device_token = sdk_runtime.sign_device_token
user_revocation_keys = sdk_runtime.user_revocation_keys


class PairingTokenVerifier(sdk_runtime.PairingTokenVerifier):
    """Agent-facing verifier that preserves eidolon_agent error types."""

    async def verify(self, token: str) -> VerifiedDevice:
        try:
            return await super().verify(token)
        except sdk_runtime.RuntimeTokenRevokedError as exc:
            raise TokenRevokedError(exc.message) from exc
        except sdk_runtime.RuntimeUnauthenticatedError as exc:
            raise UnauthenticatedError(exc.message) from exc


__all__ = [
    "PairingTokenVerifier",
    "VerifiedDevice",
    "device_revocation_keys",
    "sign_device_token",
    "user_revocation_keys",
]
