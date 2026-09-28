"""Smart-home infrastructure adapters exposed to application composition."""
from .hub import HubSmartHomeClient
from .llm_fallback import LlmHomeFallback
__all__ = ["HubSmartHomeClient", "LlmHomeFallback"]
