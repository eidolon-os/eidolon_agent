"""Domain layer — business logic. Depends only on ``core/``.

The agent's behavior lives here: Turn orchestration, persona lifecycle,
context compilation, guardrails, tools, dispatching to external workers,
realtime signal fusion, proactive triggers, conversation history, and
lifecycle hooks. None of these modules touch external systems directly;
all I/O goes through ports declared in ``core.ports``.
"""
