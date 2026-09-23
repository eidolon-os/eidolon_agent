# Optional presentation failure isolation

BOX3's 2026-09-23 11:03:02 and 11:03:14 turns reached Agent but failed with
SUCCESS_INTENT_REQUIRES_COMPLETED_OUTCOME. The strict expression validator rejected
confirm/celebrate without a completed tool outcome; TurnEngine propagated that
failure before emitting the independently generated public answer.

TurnEngine now resolves language and optional presentation separately. The strict
validator still rejects unsupported success expressions. If the response envelope
and public answer remain valid with the rejected presentation omitted, the answer
continues through the existing output guardrail, DELTA and DONE flow. The optional
intent becomes none with no outcome reference. Metadata records the rejection and
its reason separately from unconfirmed language delivery; no expression feedback
wait is started. There is no extra model call, regex text rewrite, fabricated tool
result or fallback claim of successful execution.

This is output failure isolation, not a new factuality validator for prose. Invalid
language envelopes still fail; a response with no independently deliverable answer
still fails rather than recording a nonexistent answer as successful. Task result
truth remains owned by tool outcomes. Valid expression-only and explicitly silent
responses retain their existing behavior.

Validation: Agent functional suite 44 passed. Channel presentation and real local
gRPC stream contract suites 59 passed. New turn regressions cover unsupported
confirm/celebrate, unknown expression intent, successful language completion with
rejected expression, and rejection of malformed public text. Physical queue
congestion is a separate open investigation; this fix is not evidence of its cure.
