# Smart-home model-first interpretation (2026-10-08)

The Laya configuration now uses one model request per turn. `RulesInterpreter(require_complete=True)` no longer witnesses or gates Laya. Explicit device names and room references do not skip inference. The legacy `laya.continuation` configuration key is accepted for compatibility but does not select a separate runtime path. The explicit `interpreter=rules` mode remains available for installations without a model; it is not a prefilter in Laya mode.

## Responsibility and flow

ASR → owner/session authorization → bounded context → Laya → validated proposal or LLM handoff → fresh registry/capability/parameter checks → Hub execution/receipt → context update. Neither model has direct execution authority. Device IDs, capability mappings, numeric/unit parsing, permissions, bounds, deadlines and idempotency remain deterministic. Removing the semantic gate does not remove those checks.

Laya is a choice encoder, not a free-form planner. A single HTTP request batches the existing current-sentence intent/device/action questions with full-directory contextual target disambiguation and, when structured pending/focus facts exist, pick/follow. Current-sentence questions receive only the current sentence. Contextual target selection receives recent history. Pick/follow receive the immediate pending/focus facts; history is not copied indiscriminately into every question. Different question states require the model service's `features.question_state=true`; absent support or any input truncation causes abstention.

The adapter never narrows the full device directory by parsing the user's words. A pick must select a pending candidate; an accepted follow must agree with contextual target selection and cannot override a conflicting explicit model target. Model disagreements and unresolved pending requests go to the LLM. No second single-sentence model call is stacked after continuation.

## Context

`smarthome.context_turns` defaults to 3 completed exchanges and accepts 1–5. The current utterance is separate. Records include utterance, assistant result, outcome and structured proposal; the active pending action and candidate set are separate from history. The existing 30-second semantic-context expiry remains. Session/owner isolation, closure and revision checks remain. Snapshots are copied; later turns cannot mutate earlier model requests.

The model service still enforces per-question token budgets (512 in the Mac replay). A large window/directory can overflow; truncated inputs never authorize execution. This revision bounds history by exchanges and the SDK context by 16 KiB; it does not promise that every 256-device registry fits the encoder or silently discard candidates to make it fit.

## Escalation and thresholds

`context.laya_handoff` contains model revision, status, proposed target/action, question choices/top scores and a concrete upgrade reason. The LLM receives the original utterance and context too. The prompt states that the proposal is untrusted evidence: the LLM can complete or reject it. Relative changes must use supported step/delta, not guessed absolute setpoints. Context expiry during model or LLM inference cannot authorize a stale operation.

The new `laya-smarthome-context-v2` contract is not a newly trained/calibrated model. Mac development probes found high-confidence quoted-command errors in both c4 and c10 once the lexical witness was removed. As an explicit conservative acceptance policy, direct results require every relevant score ≥ max(configured threshold, 0.99); contextual pick/follow use ≥ max(configured threshold, 0.95), and follow additionally requires a current control intent score ≥ 0.99. Scores are not calibrated correctness probabilities. This trades direct coverage for fewer unsafe acceptances; it is not evidence of general safety or an optimization of LLM latency. Do not lower floors based on the 20 observed turns alone.

## Execution and conversation behavior

- `fan_speed.step(delta)` is evaluated by the Provider using current speed; repeating “调大一点儿” repeats a delta instead of setting the same guessed absolute value. Hub owns retry idempotency and durable receipts.
- Cancelling a pending request clears the pending action. Cancelling after an execution reports the previous result and explicitly says it has not been undone; it does not invert the old command.
- Complete inputs in a session execute FIFO. Arrival of another complete input no longer invalidates an earlier independent turn. Session closure still invalidates interpretation. Queue time is logged separately, and every dequeued turn is authorized before interpretation. This does not remove LiveKit/VAD finalization delay before the handler.

## Verification and limits

Unit/contract tests cover model-first admission, full-directory target changes, three-turn snapshots, expired context, LLM evidence transport, contradiction/truncation/old-service fallback, FIFO ordering, cancellation feedback and relative device state. Mac FP32 inference replays are in sibling `eidolon_models/laya/evals/model-first-20261008` with fixed revisions c4 `7b695ba8` and c10 `e8254243`.

The real-model replay uses a scripted fallback oracle and an in-memory test actuator. It measures Laya and application behavior, not real LLM accuracy, ASR quality, Korvo rendering or NPU latency. No weights were trained or deployed. Code changes require compatible SDK, Agent, Hub, Channel and model service updates; deploy only after the separately authorized board validation.
