"""Errors the smart-home ports raise."""

from __future__ import annotations


class SmartHomeUnavailable(Exception):
    """The Capability Runtime could not be reached, so nothing was executed.

    A port raises this only when the request certainly never took effect; a
    reply lost after sending must surface as a timeout (outcome ``unknown``).
    """
