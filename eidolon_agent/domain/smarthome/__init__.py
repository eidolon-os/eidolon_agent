"""Smart-home domain: spoken commands and LLM tools over the Owner's home.

Understanding goes through the InteractionInterpretation port; execution
through the Capability Runtime ports below. Neither owns the registry or
device state: System Data and the Providers do.
"""

from eidolon_agent.domain.smarthome.command import SmartHomeCommand, interpretation_request
from eidolon_agent.domain.smarthome.context import HomeCancellation, HomeClarification, HomeContext, HomeUnderstanding
from eidolon_agent.domain.smarthome.errors import SmartHomeUnavailable
from eidolon_agent.domain.smarthome.ports import (
    DeviceStatus,
    HomeSnapshot,
    SmartHomeDirectoryPort,
    SmartHomeExecutePort,
    SmartHomeFallbackPort,
)
from eidolon_agent.domain.smarthome.tools import smart_home_tools

__all__ = [
    "DeviceStatus",
    "HomeCancellation",
    "HomeClarification",
    "HomeContext",
    "HomeUnderstanding",
    "HomeSnapshot",
    "SmartHomeCommand",
    "SmartHomeDirectoryPort",
    "SmartHomeExecutePort",
    "SmartHomeFallbackPort",
    "SmartHomeUnavailable",
    "interpretation_request",
    "smart_home_tools",
]
