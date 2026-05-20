"""Application-level configuration.

The single source of truth at runtime is :class:`~eidolon_agent.config.settings.Settings`,
loaded from a YAML file overlaid with environment variables.
"""

from eidolon_agent.config.settings import Settings, get_settings, load_settings

__all__ = ["Settings", "get_settings", "load_settings"]
