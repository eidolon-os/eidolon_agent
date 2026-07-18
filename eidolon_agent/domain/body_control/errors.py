"""Errors raised by the body-control domain service."""

from __future__ import annotations


class BodyControlError(Exception):
    code = "body_control_error"


class BodyControlUnavailable(BodyControlError):
    code = "body_control_unavailable"


class BodyDeviceNotFound(BodyControlError):
    code = "body_device_not_found"


class BodyDeviceAmbiguous(BodyControlError):
    code = "body_device_ambiguous"

    def __init__(self, target: str, matches: list[str]) -> None:
        super().__init__(f"ambiguous body device target {target!r}: {', '.join(matches)}")
        self.target = target
        self.matches = matches


class BodyCompanionNotFound(BodyControlError):
    code = "body_companion_not_found"


class BodyCompanionAmbiguous(BodyControlError):
    code = "body_companion_ambiguous"

    def __init__(self, target: str, matches: list[str]) -> None:
        super().__init__(
            f"ambiguous companion target {target!r}: {', '.join(matches)}"
        )
        self.target = target
        self.matches = matches


class BodyDeviceOffline(BodyControlError):
    code = "body_device_offline"


class BodyCapabilityUnsupported(BodyControlError):
    code = "body_capability_unsupported"


class BodyCommandRejected(BodyControlError):
    code = "body_command_rejected"
