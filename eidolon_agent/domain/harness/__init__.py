"""Realtime agent harness boundary.

The harness is the runtime shell around the LLM: prompt policy, context
contract, tool exposure, and prompt-safe trace shape. There is intentionally
one harness for the realtime agent; cowork is a tool the harness may expose,
not a separate profile.
"""

from eidolon_agent.domain.harness.realtime import (
    HARNESS_POLICY_SOURCE,
    HarnessBudget,
    HarnessSnapshot,
    RealtimeAgentHarness,
    realtime_harness_policy_prompt,
)

__all__ = [
    "HARNESS_POLICY_SOURCE",
    "HarnessBudget",
    "HarnessSnapshot",
    "RealtimeAgentHarness",
    "realtime_harness_policy_prompt",
]
