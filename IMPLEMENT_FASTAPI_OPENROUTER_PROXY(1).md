# IMPLEMENT_FASTAPI_OPENROUTER_PROXY.md

> Revised implementation specification for a FastAPI → OpenRouter LLM gateway with conservative expert-model routing.
>
> The backend owns concrete model IDs and cost policy.
>
> The mobile app sends only:
>
> ```text
> fast
> normal
> expert
> ```
>
> The `expert` tier may currently map to Terra, but the mobile app must never depend on that name.

---

# 0. Goal

Architecture:

```text
Android AgentRuntime
      │
      │ model_tier
      │ routing_context
      ▼
FastAPI
      │
      ├─ auth
      ├─ quotas
      ├─ deterministic routing policy
      ├─ expert budget gate
      └─ OpenRouter proxy
      │
      ▼
OpenRouter
      │
      ▼
FAST / NORMAL / EXPERT model
```

Tool execution remains local:

```text
LLM
 ↓ tool call
FastAPI
 ↓
Android
 ↓
MCP
 ↓
Policy / Approval
 ↓
Connector
```

The FastAPI proxy must not execute MCP tools.

---

# 1. Routing philosophy

The most expensive model must be rare.

Do not implement:

```text
2 connectors → expert
3 MCP calls → expert
large result → expert
```

Instead:

```text
expert =
  high reasoning score
  AND hard reasoning signal

OR

  cheaper planner is demonstrably stuck
```

The backend should enforce this even if a buggy frontend asks for `expert` too aggressively.

---

# 2. Three tiers

Use:

```py
ModelTier = Literal[
    "fast",
    "normal",
    "expert",
]
```

Backend configuration:

```env
LLM_MODEL_FAST=
LLM_MODEL_NORMAL=
LLM_MODEL_EXPERT=
```

Example semantics:

```text
FAST
high-volume/default model

NORMAL
moderate reasoning

EXPERT
rare expensive model
currently may map to Terra
```

Do not expose concrete model IDs to the mobile app.

---

# 3. Cost objective

Target distribution:

```text
FAST    ~80-90%
NORMAL  ~8-18%
EXPERT  ~1-3%
```

This is a telemetry target.

Do not enforce it by randomly rejecting requests.

If expert usage becomes high, investigate routing rules.

---

# 4. Backend structure

Recommended:

```text
app/
  main.py

  api/routes/
    agent.py

  core/
    config.py
    security.py
    logging.py

  llm/
    schemas.py
    gateway.py
    routing.py
    reasoning.py
    expert_budget.py
    openrouter.py
    errors.py
    usage.py

  tests/
    test_agent_api.py
    test_model_routing.py
    test_expert_budget.py
    test_openrouter_client.py
    test_privacy.py
```

Adapt to the existing backend layout.

---

# 5. Settings

Example:

```py
class Settings(BaseSettings):
    openrouter_api_key: str

    llm_model_fast: str
    llm_model_normal: str
    llm_model_expert: str

    llm_normal_score_threshold:
        int = 4

    llm_expert_score_threshold:
        int = 10

    llm_allow_expert:
        bool = True

    llm_expert_daily_user_limit:
        int = 20

    llm_expert_global_daily_budget_usd:
        float | None = None

    llm_request_timeout_seconds:
        float = 60.0

    llm_max_retries:
        int = 1
```

Do not return these settings to normal clients.

---

# 6. Public endpoint

Use:

```text
POST /agent/step
```

One request = one model turn.

The mobile AgentRuntime owns the multi-step execution loop.

This keeps connector credentials and MCP execution on the client side.

---

# 7. Routing request schema

Use a richer routing context.

```py
class WeakExecutionSignals(BaseModel):
    step_count: int = 0
    tool_calls: int = 0
    connector_count: int = 0


class StruggleSignals(BaseModel):
    failed_plans: int = 0
    replans: int = 0

    repeated_tool_pattern:
        bool = False

    invalid_tool_calls:
        int = 0

    repeated_tool_failures:
        int = 0


class ContextSignals(BaseModel):
    large_structured_context:
        bool = False

    large_unstructured_context:
        bool = False


class RoutingContext(BaseModel):
    reasoning_score:
        int = 0

    hard_reasoning_signals:
        list[str] = []

    weak_signals:
        WeakExecutionSignals

    struggle:
        StruggleSignals

    context:
        ContextSignals

    escalation_count:
        int = 0
```

Use `Field(default_factory=list)` in real code for mutable defaults.

---

# 8. Allowed hard reasoning signal names

Do not accept arbitrary strings forever.

Normalize to an enum.

```py
HardReasoningSignal = Literal[
    "cross_source_synthesis",
    "conflicting_evidence",
    "constraint_solving",
    "temporal_reconciliation",
    "ranking_or_optimization",
    "dependent_multi_stage_reasoning",
]
```

Struggle signals are separate.

---

# 9. Expert selection rule

Create:

```text
app/llm/routing.py
```

The backend should not simply trust:

```json
{
  "model_tier": "expert"
}
```

Compute an effective tier.

Pseudo-policy:

```py
def select_effective_tier(
    requested_tier: ModelTier,
    ctx: RoutingContext,
    settings: Settings,
) -> ModelTier:
    ...
```

Recommended rules:

```text
1. If expert disabled:
   never return expert.

2. If emergency struggle trigger:
   expert may be allowed.

3. Else expert requires:
   reasoning_score >= expert threshold
   AND at least one hard reasoning signal.

4. Normal requires:
   requested normal/expert
   OR score >= normal threshold
   OR at least one replan.

5. Otherwise:
   fast.
```

---

# 10. Emergency expert triggers

Backend should recognize:

```text
failed_plans >= 2
repeated_tool_pattern == true
invalid_tool_calls >= 3
replans >= 2
```

Example:

```py
def has_emergency_expert_trigger(
    ctx: RoutingContext,
) -> bool:
    s = ctx.struggle

    return (
        s.failed_plans >= 2
        or s.repeated_tool_pattern
        or s.invalid_tool_calls >= 3
        or s.replans >= 2
    )
```

This lets expert rescue a stuck run.

---

# 11. Hard signal gate

```py
def has_hard_reasoning_signal(
    ctx: RoutingContext,
) -> bool:
    return bool(
        ctx.hard_reasoning_signals
    )
```

Normal expert condition:

```py
expert_allowed_by_reasoning = (
    ctx.reasoning_score
    >= settings
        .llm_expert_score_threshold
    and has_hard_reasoning_signal(ctx)
)
```

---

# 12. Do not trust weak signals for expert

Never do this:

```py
if ctx.weak_signals.connector_count >= 3:
    return "expert"
```

or:

```py
if ctx.weak_signals.tool_calls >= 5:
    return "expert"
```

Weak signals may influence:

```text
fast → normal
```

but should not directly produce expert.

---

# 13. Backend verification of routing context

The frontend calculates reasoning signals, but backend should apply basic sanity checks.

Examples:

```text
reasoning_score range
signal enum validation
non-negative counters
reasonable upper limits
```

Reject malformed routing metadata.

Do not accept:

```text
reasoning_score = 999999
```

A recommended bound:

```text
0 <= reasoning_score <= 100
```

---

# 14. Requested tier is only a hint

Examples:

## Client asks FAST

```text
reasoning_score = 12
hard signal = constraint_solving
```

Backend may override:

```text
effective = EXPERT
```

## Client asks EXPERT

```text
reasoning_score = 2
hard signals = []
struggle = none
```

Backend should usually downgrade:

```text
effective = FAST or NORMAL
```

This protects cost.

---

# 15. Expert budget gate

Create:

```text
app/llm/expert_budget.py
```

Use an interface:

```py
class ExpertBudgetService:
    async def can_use_expert(
        self,
        *,
        user_id: str,
        run_id: str,
    ) -> bool:
        ...

    async def record_expert_use(
        self,
        *,
        user_id: str,
        run_id: str,
        estimated_cost_usd:
            float | None,
    ) -> None:
        ...
```

Possible limits:

```text
per-user daily expert calls
per-user daily expert tokens
global daily expert budget
plan-based expert entitlement
```

Do not add unnecessary billing infrastructure if not yet needed.

Start with configurable counters.

---

# 16. Expert budget behavior

If expert is justified but budget is unavailable:

```text
try NORMAL
```

Return:

```json
{
  "requested_model_tier":
    "expert",

  "effective_model_tier":
    "normal",

  "routing_reason":
    "expert_budget_unavailable"
}
```

Do not silently lie that expert was used.

---

# 17. Per-run expert stickiness

Once a run is escalated to expert, you have two competing goals:

```text
avoid oscillation
avoid using expert for every trivial continuation
```

Recommended MVP:

```text
expert remains effective for the rest of the current reasoning episode
```

But allow the frontend to mark a new independent subtask/run.

Do not downgrade and upgrade every turn.

This reduces routing instability.

---

# 18. Optional expert-call cap per run

To prevent runaway cost:

```env
LLM_EXPERT_MAX_CALLS_PER_RUN=3
```

If exceeded:

```text
continue NORMAL
or return a controlled error
```

depending on product policy.

Do not allow a broken loop to spend unlimited expert tokens.

---

# 19. Model selection object

```py
@dataclass(frozen=True)
class ModelSelection:
    requested_tier: ModelTier

    effective_tier: ModelTier

    model_id: str

    reason: str

    expert_budget_used:
        bool
```

Reasons:

```text
default_fast
moderate_reasoning
hard_reasoning
planner_stuck
expert_budget_unavailable
expert_disabled
provider_fallback
```

---

# 20. Request schema

Example:

```py
class AgentStepRequest(BaseModel):
    request_id: str
    run_id: str

    model_tier:
        ModelTier = "fast"

    routing_context:
        RoutingContext

    messages:
        list[AgentMessage]

    tools:
        list[AgentTool] = Field(
            default_factory=list,
        )
```

Do not accept a concrete model ID from normal mobile clients.

---

# 21. Response schema

```py
class AgentStepResponse(BaseModel):
    request_id: str
    run_id: str

    requested_model_tier:
        ModelTier

    effective_model_tier:
        ModelTier

    routing_reason:
        str

    message:
        AgentMessage

    usage:
        UsageInfo | None = None
```

In development, optional:

```text
model_alias
latency_ms
provider_request_id
```

Avoid exposing unnecessary provider internals in production.

---

# 22. Model aliases

Use stable aliases:

```text
fast-v1
normal-v1
expert-v1
```

Mapping:

```py
MODEL_ALIASES = {
    "fast": {
        "alias": "fast-v1",
        "model_id":
            settings.llm_model_fast,
    },

    "normal": {
        "alias": "normal-v1",
        "model_id":
            settings.llm_model_normal,
    },

    "expert": {
        "alias": "expert-v1",
        "model_id":
            settings.llm_model_expert,
    },
}
```

This allows replacing Terra later without changing mobile code or analytics semantics.

---

# 23. OpenRouter proxy

Keep the existing architecture:

```text
shared httpx.AsyncClient
FastAPI lifespan
server-side OpenRouter API key
normalized request/response
```

The OpenRouter key must never enter:

```text
APK
mobile .env
tool schema
LLM prompt
logs
```

---

# 24. Provider request

Concept:

```py
payload = {
    "model":
        selection.model_id,

    "messages":
        provider_messages,
}

if provider_tools:
    payload["tools"] = (
        provider_tools
    )
```

Provider routing/privacy options remain backend config.

Verify current OpenRouter field names against official docs when implementing.

---

# 25. No second routing LLM initially

Do not implement:

```text
router LLM
→ model selection
→ actual model
```

This adds cost and latency.

Use:

```text
frontend runtime evidence
+
backend deterministic policy
+
budget gate
```

If later evals show a classifier provides measurable value, add it intentionally.

---

# 26. Trust boundary

Frontend can report:

```text
hard reasoning signals
reasoning score
struggle counters
```

Backend treats them as advisory.

Backend owns:

```text
effective tier
model ID
budget
quota
provider fallback
```

Do not let a modified client freely spend expert-model quota.

---

# 27. Authentication

`POST /agent/step` must require authenticated app identity.

Bind usage to server-derived:

```text
user_id
```

Optional:

```text
device_id
subscription plan
```

Do not trust `user_id` inside request JSON.

---

# 28. Rate limits

At minimum:

```text
global requests/min
per-user requests/min
concurrent requests/user
daily token budget
expert-specific daily budget
expert calls/run
```

Expert should have stricter limits than fast.

---

# 29. Retry rules

LLM generation has no external side effect by itself.

Limited retry can be allowed for transient provider failures.

Suggested:

```text
1 retry max
```

Do not retry indefinitely.

Do not retry validation/auth failures.

---

# 30. Provider fallback by tier

Example:

```text
FAST primary
→ FAST fallback

NORMAL primary
→ NORMAL fallback
→ optional FAST degradation

EXPERT primary
→ EXPERT fallback
→ NORMAL degradation
```

If expert falls back to normal:

```text
effective_model_tier = normal
```

Report it.

---

# 31. Do not use expert because provider failed

Provider failure is not reasoning complexity.

If FAST provider fails:

```text
use FAST fallback
```

not:

```text
jump to Terra
```

Otherwise provider outages become cost explosions.

---

# 32. Telemetry

Store safe metadata:

```text
request_id
run_id
user_id

requested tier
effective tier
routing reason

reasoning score
hard signal names

step count
tool call count
connector count

failed plan count
replan count
loop detected

prompt tokens
completion tokens
latency
status
```

Do not store private message/tool-result content by default.

---

# 33. Expert-specific metrics

Track:

```text
expert_requests_total

expert_allowed_total

expert_downgraded_total

expert_budget_denied_total

expert_rescue_success_total

expert_calls_per_run

expert_token_cost
```

Most useful:

```text
expert_rescue_rate
```

Definition:

```text
runs that were stuck/complex
and succeeded after expert
/
runs escalated to expert
```

---

# 34. Cost-quality metric

Optimize:

```text
successful agent task
/
total model cost
```

not:

```text
cheapest single model call
```

An expert call is justified when it materially increases completion probability.

---

# 35. Expert audit log

For each expert selection store:

```text
reasoning score
hard signals
emergency trigger
prior tier
step number
budget status
outcome
```

Example:

```json
{
  "requested_tier":
    "normal",

  "effective_tier":
    "expert",

  "reason":
    "planner_stuck",

  "reasoning_score":
    8,

  "hard_signals": [],

  "failed_plans":
    2,

  "step":
    6,

  "completed_successfully":
    true
}
```

No private prompt text required.

---

# 36. Privacy

Do not log:

```text
Telegram message bodies
email bodies
OAuth tokens
Telegram codes
2FA passwords
OpenRouter API key
Authorization headers
cookies
```

Routing logs only need metadata.

---

# 37. Request limits

Protect paid provider usage.

Validate:

```text
message count
tool count
tool description length
JSON body size
reasoning score range
counter ranges
routing signal enum values
```

Reject pathological requests before OpenRouter.

---

# 38. Expert abuse protection

A malicious modified client could send:

```json
{
  "model_tier": "expert",
  "reasoning_score": 100,
  "hard_reasoning_signals": [
    "constraint_solving"
  ]
}
```

Therefore expert authorization should also consider:

```text
authenticated user quota
run history if stored
expert call limit
server policy
```

For MVP, deterministic validation + strict expert quota is sufficient.

Later, server-side run metadata can strengthen verification.

---

# 39. Optional server-side run state

If needed, persist minimal routing state:

```text
run_id
user_id
last effective tier
expert calls
last reasoning score
last step number
```

This allows the backend to detect suspicious jumps such as:

```text
brand new run
step 1
expert requested
score 100
```

Do not persist full conversation unless product requirements need it.

---

# 40. Idempotency

Use:

```text
user_id + request_id
```

for short-lived response deduplication.

A duplicated `/agent/step` request should not create unnecessary duplicate provider cost when the original response is already available.

This is separate from MCP action idempotency.

---

# 41. Cancellation

When Android cancels:

```text
disconnect/cancel FastAPI request
→ cancel upstream httpx request where possible
```

Do not keep expensive expert generation running unnecessarily.

---

# 42. Streaming

Implement non-streaming first.

After tool calling and routing are stable:

```text
POST /agent/step/stream
```

Normalize provider chunks.

Do not execute tool arguments until the complete tool call is assembled and validated on the client/MCP side.

---

# 43. Tests: routing

Required cases:

## Easy long task

```text
tool_calls = 8
connector_count = 2
reasoning_score = 2
hard signals = []
```

Expected:

```text
FAST or NORMAL
never EXPERT
```

## Hard short task

```text
tool_calls = 2
reasoning_score = 12
hard signals = [
  "constraint_solving",
  "ranking_or_optimization"
]
```

Expected:

```text
EXPERT
```

## Cross connector but sequential

```text
Telegram send
Calendar create
```

Expected:

```text
not EXPERT
```

## Cross-source synthesis

```text
Telegram evidence
Gmail evidence
Calendar evidence
```

with:

```text
cross_source_synthesis
```

Expected according to score:

```text
NORMAL or EXPERT
```

## Client asks expert without evidence

Expected:

```text
downgrade
```

## Planner stuck

```text
failed_plans = 2
```

Expected:

```text
EXPERT
```

## Loop

```text
repeated_tool_pattern = true
```

Expected:

```text
EXPERT
```

## Expert disabled

Expected:

```text
NORMAL fallback
```

## Expert budget denied

Expected:

```text
NORMAL fallback
routing reason recorded
```

---

# 44. Tests: provider failure

FAST model provider fails.

Expected:

```text
FAST fallback
```

not EXPERT.

NORMAL provider fails.

Expected:

```text
NORMAL fallback
or documented degradation
```

EXPERT provider fails.

Expected:

```text
EXPERT fallback
or NORMAL degradation
```

No uncontrolled tier upgrades because of infrastructure errors.

---

# 45. Tests: privacy

Assert logs do not contain known fake:

```text
OPENROUTER_API_KEY
OAuth token
Telegram code
Authorization header
private message body
```

---

# 46. Suggested routing implementation

Concept:

```py
def select_effective_tier(
    requested_tier: ModelTier,
    ctx: RoutingContext,
    settings: Settings,
    expert_budget_ok: bool,
) -> tuple[ModelTier, str]:

    emergency = (
        ctx.struggle.failed_plans
            >= 2
        or ctx.struggle
            .repeated_tool_pattern
        or ctx.struggle
            .invalid_tool_calls
            >= 3
        or ctx.struggle.replans
            >= 2
    )

    hard_reasoning = bool(
        ctx.hard_reasoning_signals
    )

    expert_by_reasoning = (
        ctx.reasoning_score
        >= settings
            .llm_expert_score_threshold
        and hard_reasoning
    )

    expert_justified = (
        emergency
        or expert_by_reasoning
    )

    if (
        expert_justified
        and settings.llm_allow_expert
        and expert_budget_ok
    ):
        return (
            "expert",
            (
                "planner_stuck"
                if emergency
                else "hard_reasoning"
            ),
        )

    if (
        requested_tier
        in ("normal", "expert")
        or ctx.reasoning_score
        >= settings
            .llm_normal_score_threshold
        or ctx.struggle.replans >= 1
    ):
        return (
            "normal",
            (
                "expert_budget_unavailable"
                if expert_justified
                else "moderate_reasoning"
            ),
        )

    return (
        "fast",
        "default_fast",
    )
```

Production code should separate the policy into testable helpers.

---

# 47. Do not use exact percentages as router logic

Do not write:

```py
if expert_usage_today > 3%:
    deny expert
```

That can reduce quality during genuinely hard workloads.

Instead use:

```text
budgets
quotas
thresholds
telemetry
```

The percentage is an operational signal.

---

# 48. Definition of done

```text
[ ] Three model tiers exist.
[ ] Concrete model IDs exist only on backend.
[ ] EXPERT is not triggered by tool count alone.
[ ] EXPERT is not triggered by connector count alone.
[ ] EXPERT is not triggered by large context alone.
[ ] Hard reasoning signal gate exists.
[ ] Expert score threshold exists.
[ ] Emergency stuck-planner triggers exist.
[ ] Backend can downgrade unjustified expert requests.
[ ] Expert budget/quota gate exists.
[ ] Expert calls/run can be capped.
[ ] Provider failures do not automatically upgrade tiers.
[ ] Routing reason is returned.
[ ] Expert telemetry is privacy-safe.
[ ] Tests cover easy-long and hard-short tasks.
[ ] Tests cover malicious/unjustified expert requests.
[ ] Tests cover budget-denied expert fallback.
```

---

# 49. Final routing principle

The backend should answer this question:

```text
"Is the expensive model likely to materially improve
the probability of completing this reasoning task?"
```

not:

```text
"Has the agent already called several tools?"
```

Use expert/Terra for:

```text
conflicts
constraints
optimization
cross-source synthesis
dependent multi-stage reasoning
repeated replanning
planner loops
failed cheaper-model plans
```

Do not use it merely for:

```text
many MCP calls
many connectors
long deterministic workflows
multiple writes
large structured results
```

The expensive model is a **reasoning rescue / expert tier**, not a default multi-tool model.
