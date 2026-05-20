"""Device pairing — pairing_code → device_token exchange + token verification."""

from eidolon_agent.transport.pairing.coordinator import PairingCoordinator
from eidolon_agent.transport.pairing.token import (
    PairingTokenVerifier,
    VerifiedDevice,
    sign_device_token,
)

__all__ = [
    "PairingCoordinator",
    "PairingTokenVerifier",
    "VerifiedDevice",
    "sign_device_token",
]
