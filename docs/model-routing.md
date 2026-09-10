# Model Routing (Milestone U)

`patchfrog.routing` chooses **how** an already-scheduled piece of review
work is executed -- which credentialed provider serves the reviewer
role(s) and the critic for one review run. It never decides **whether**
a finding is true (that stays with the specialist/critic/verifier
pipeline, see `docs/agent-orchestration.md`), and it never creates a
second review engine.

## U1 -- OpenAI provider adapter

`patchfrog.review.providers.openai_provider.OpenAILLMProvider` is a
third implementation of the existing, unchanged
`patchfrog.review.provider.LLMProvider` protocol -- no parallel
interface. It uses the official `openai` Python SDK (pinned
`>=3.11,<4.0`, Apache-2.0) against the **Responses API**
(`client.responses.create`), OpenAI's current recommended path (the
legacy Chat Completions endpoint is not used). Structured output is
enforced via `text={"format": {"type": "json_schema", "schema": ...,
"strict": True}}` -- PatchFrog's schemas are already plain JSON Schema
dicts with `additionalProperties: false`, so `strict=True` applies
unconditionally, exactly parallel to the Anthropic (`output_format`) and
Gemini (`response_json_schema`) adapters.

Failure classification mirrors the other two adapters: `RateLimitError`/
`APIConnectionError` (which `APITimeoutError` subclasses)/
`InternalServerError` -> `ProviderTransientError`; any other
`APIStatusError` (400/401/403/404/422) -> `ProviderFatalError`. A
refusal content item, or `status == "incomplete"` with
`incomplete_details.reason == "content_filter"`, is treated as a
provider refusal (`ProviderFatalError`); `"max_output_tokens"` truncation
is left to fail JSON/schema parsing naturally downstream, exactly like a
truncated Anthropic/Gemini response. The SDK's own default retry (2
attempts) is disabled per-call, the same reasoning as the other two
adapters' own SDK-retry disable -- PatchFrog's orchestrator applies its
own bounded retry on top.

The installed `openai` SDK is built on `httpx2`, a distinct package from
the classic `httpx` every other adapter (and `respx`) in this codebase
mocks -- contract tests inject an `httpx2.MockTransport`-backed client
via the adapter's own test-only `http_client` constructor parameter
instead (see `tests/unit/test_review_openai_provider_contract.py`).

**No live OpenAI calls anywhere in this milestone** -- every test uses a
mocked transport.

## U2 -- Provider capability registry

`patchfrog.routing.capabilities` deliberately encodes exactly one fact:
whether a provider's adapter guarantees bounded, schema-conforming
structured output (`structured_output: bool`, true for all three
adapters today). It does **not** encode reasoning strength, cost tier,
or context-window size as static per-vendor facts -- those are marketing
claims that go stale the moment a vendor ships a new model, not
capabilities the router's own code can verify or needs to route safely.
An operator who wants a specific provider for a specific role configures
that directly (see U3/U4 below), rather than PatchFrog guessing which
vendor is "stronger" from a hardcoded table.

## U3/U4 -- Deterministic routing, reviewer/critic family diversity

`patchfrog.routing.router.ModelRouter.route(*, runtime_config,
critic_enabled)` computes one `ReviewRoutePlan`
(`patchfrog.routing.domain`) **once per review run** -- not once per
candidate. `patchfrog.review.orchestration.AgentOrchestrator` already
took a `reviewer_providers: Mapping[AgentRole, LLMProvider]` constructor
parameter before this milestone (a per-role provider mapping was already
the real dispatch shape); the router's whole job is to produce that
mapping (plus a critic provider) from operator policy, so no
orchestration change was needed. `PullRequestReviewService` gained an
additive `route_plan: ReviewRoutePlan | None = None` constructor
parameter -- when given, it governs every provider call for that run;
`None` (the default) preserves the exact pre-Milestone-U single-provider
behavior unchanged.

**Why per-run, not per-candidate**: `AgentOrchestrator` is itself
constructed once per run, and the existing `ReviewModelIdentity`
provenance record persisted onto `ReviewRunModel` is a run-level record
(`reviewer_provider`, `reviewer_model`, `critic_provider`,
`critic_model`). Varying provider per candidate would require moving
orchestrator construction inside the per-candidate loop (which the
existing token-budget reservation locking is not designed around) plus a
schema change for per-finding provider provenance -- a materially larger,
separate change, deferred as an honest v1 scope boundary rather than a
silent gap.

**Selection logic**:

1. Every `SUPPORTED_PROVIDERS` entry (`anthropic`, `gemini`, `openai`)
   with a structured-output-capable adapter *and* a non-empty API key
   (`ANTHROPIC_API_KEY`/`GEMINI_API_KEY`/`OPENAI_API_KEY`) is
   "configured." No provider configured at all ->
   `NoProviderConfiguredError` (a subclass of the existing
   `MissingProviderCredentialsError`, not a parallel exception type).
2. Reviewer role(s): the operator-preferred provider
   (`PATCHFROG_REVIEW_PROVIDER`, via the already-resolved
   `ReviewRuntimeConfig.provider`) if configured; otherwise the single,
   explicitly-configured `PATCHFROG_ROUTER_FALLBACK_PROVIDER` if that is
   configured instead; otherwise `MissingProviderCredentialsError`.
   Fallback is bounded to exactly one hop -- there is no chain to a
   second fallback, and no retry-driven re-routing to a third provider.
3. Critic: disabled entirely when `critic_enabled=False`
   (repository-controlled review behavior, unrelated to routing). With
   exactly one provider configured, the critic uses that same family
   (diversity is structurally unavailable, not merely unused). With more
   than one configured, an explicit `PATCHFROG_ROUTER_CRITIC_PROVIDER`
   is honored if it names a configured provider; otherwise the router
   auto-selects any other configured provider for family diversity
   (Correctness/Security proposed by one model family, verified by a
   different one, when the operator's own deployment makes that
   possible) -- **diversity is never required** when only one provider
   is configured; self-hosting with a single provider still works
   unchanged.
4. When the critic runs on a different family than the reviewer and the
   operator didn't also set `PATCHFROG_REVIEW_CRITIC_MODEL` to a model
   name valid for that other family, `patchfrog.review.runtime_config.DEFAULT_MODEL_BY_PROVIDER`
   supplies a reasonable per-provider default rather than sending the
   reviewer's own (likely invalid-for-that-provider) model name.

**Routing never depends on repository content.** Every input to
`ModelRouter.route` is operator/deployment-controlled
(`patchfrog.config.settings.Settings`, read once at construction) or an
already-resolved `ReviewRuntimeConfig` -- there is no diff/comment/PR-body
parameter to read at all, and `.patchfrog.yml`'s existing
`OPERATOR_ONLY_REVIEW_FIELDS` rejection list already covers
`provider`/`model`/`critic_model`/`request_timeout_seconds`; no new field
name was needed for the router (see `test_route_signature_takes_no_repository_content_parameter`).

**Zero-LLM path**: not decided inside the router at all. An empty
candidate set means the caller never invokes `ModelRouter.route` in the
first place -- there is nothing to route, so this module has no "should I
call an LLM" branch of its own to avoid duplicating that
already-existing, upstream decision.

## Runtime execution failover (pre-merge correction)

**Two distinct fallback concepts, deliberately not conflated** -- see
`patchfrog.routing.domain.ReviewRoutePlan`'s own docstring for the full
distinction:

1. **Configuration-time provider-selection fallback**
   (`ReviewRoutePlan.config_fallback_used`, described in "Selection
   logic" item 2 above): the preferred provider had no credential
   configured *at all* -- a different provider is *selected* as primary
   before any call is ever made. Decided once, by `ModelRouter.route`.
2. **Runtime execution failover**
   (`ReviewRoutePlan.reviewer_fallback_providers`/
   `critic_fallback_provider`, gated by `runtime_fallback_permitted`):
   the *selected* primary provider is credentialed and used, but an
   actual bounded review call to it fails. The router only ever
   *computes* which provider is eligible to serve as the one-hop runtime
   fallback; it never itself makes or retries a call. That happens in
   `patchfrog.review.orchestration.AgentOrchestrator._call_role`/
   `_critique_one`, which decide *when* to use the fallback the router
   named. Both concepts reuse the same single
   `PATCHFROG_ROUTER_FALLBACK_PROVIDER` setting -- one operator-configured
   backup provider, two distinct trigger points, never a second config
   surface.

**Runtime fallback is never auto-selected** (final correction): having a
credential for some other provider is not, by itself, permission to use
it as a runtime backup -- `ModelRouter._select_runtime_fallback_family`
returns a fallback family *only* when `PATCHFROG_ROUTER_FALLBACK_PROVIDER`
explicitly names one, and that provider is itself configured/credentialed
and distinct from the role's own primary. With two providers configured
but no explicit fallback setting, `runtime_fallback_permitted` is
`False` -- exactly as if only one provider were configured -- even
though critic family diversity (a separate, config-time concept,
unaffected by this) may still legitimately use the second provider.

**What triggers runtime failover** (after the primary's own existing
bounded retry allowance, `max_retries`, is exhausted):

- `ProviderTransientError` (rate limit, timeout, connection failure,
  transient server failure) -> eligible.
- `ResponseSchemaError` (the primary responded, but its output did not
  parse/validate against the schema) -> **also** eligible. This is a
  provider-compliance issue, not an authorization or request-shape
  issue -- a structurally different provider may simply honor strict
  JSON-schema mode differently, and retrying the *same* non-compliant
  provider with the identical request would not help (the same reason
  it is never retried on the same provider either).
- `ProviderFatalError` (auth failure, malformed request, refusal) ->
  **never** eligible, propagates immediately. An auth/config/refusal
  failure is not assumed safe to retry against a different vendor by
  default in v1 -- retrying elsewhere after, say, a policy refusal could
  violate the refusal's own intended semantics, and a malformed-request
  failure would very likely reproduce identically against any vendor.

**Bounded to exactly one runtime hop, never a chain, never back to the
primary**: the fallback attempt itself uses `max_retries=0` (a single
try, never its own retry loop). `AgentOrchestrator`'s
`reviewer_fallback_providers`/`critic_fallback` constructor parameters
each name exactly one provider -- there is structurally no third
provider to escalate to.

**Quality + Cost Guard integration**: the fallback's actual usage (not
an estimate) flows through the exact same reservation-then-reconcile
accounting every role's call already goes through -- never a "free" call
outside budget tracking. The check-and-reserve for the fallback hop is
**atomic** under the run's shared `budget_lock` (final correction: a
check-then-later-increment pattern would let two concurrently-running
roles' fallback attempts both pass a stale check before either actually
reserved, overspending the budget -- proven by
`test_concurrent_two_role_fallback_with_budget_room_for_one_allows_exactly_one`,
which forces genuine interleaving since `FakeLLMProvider` alone never
actually suspends). The reservation this creates is a *temporary* hold,
always released before the method returns (success or failure) --
never double-counted alongside the existing reconcile-after-actual-usage
pass, which already correctly credits the original per-role estimate
against whichever provider's real usage comes back. A denied fallback
(no budget room) behaves exactly like "no fallback configured" -- the
original error propagates, the role is honestly marked failed. The same
atomic reserve/release applies identically to the critic's own fallback
hop.

**Review completeness**: if a candidate's every selected role fails
(even after a permitted, attempted fallback), that candidate is marked
`failed=True` exactly as it always was pre-correction -- unaffected by
runtime failover existing. `PullRequestReviewService`'s own,
unmodified aggregation of per-candidate failures into the run's
`ReviewRunStatus` (`PARTIAL`/`FAILED` vs. `SUCCEEDED`) is therefore also
unaffected. This is the mechanism that keeps Merge Readiness honest:
`patchfrog.merge_readiness.service.MergeReadinessService` only ever
reads `ReviewRunStatus` and never anything about *how* a run reached
that status, so "primary failed, fallback also failed, review
incomplete" and "review failed for any other reason" both correctly
resolve to `HUMAN_REVIEW_REQUIRED` -- see
`tests/integration/test_runtime_failover_merge_readiness_integration.py`
for an end-to-end proof through a real `PullRequestReviewService.review_local`
call.

**Provenance**: `CandidateOrchestrationResult` (per-candidate, in-memory
only) gained `fallback_used_roles`/`executed_provider_by_role`/
`critic_fallback_used`/`critic_executed_provider` -- which provider
family *actually* executed each call, for observability, never
persisted to a new table and never affecting which findings survive.

**Reviewer fallback and critic family diversity remain independent
mechanisms** -- a reviewer role falling back to a backup provider says
nothing about whether the critic used a diverse family, and vice versa;
`tests/integration/test_runtime_provider_failover.py`'s own
`test_reviewer_fallback_and_critic_diversity_are_independent` proves
this explicitly.

## Operator configuration

| Env var | Meaning |
|---|---|
| `OPENAI_API_KEY` | OpenAI credential (never in `.patchfrog.yml`) |
| `PATCHFROG_REVIEW_PROVIDER` | Preferred/primary provider family (unchanged from before this milestone; now also accepts `openai`) |
| `PATCHFROG_ROUTER_FALLBACK_PROVIDER` | Optional, single fallback family if the preferred one has no credential |
| `PATCHFROG_ROUTER_CRITIC_PROVIDER` | Optional, explicit critic family (overrides auto-diversity) |

Self-host: the operator controls which providers are available and how
routing behaves. Future PatchFrog Cloud: production model-routing
weights, provider-health-fleet awareness, centralized cost optimization,
and experiment/A-B routing remain private Cloud control-plane concerns
(`docs/product-boundary.md`'s own "Managed provider/model routing" Cloud
entry) -- this milestone ships only the generic, self-host routing
algorithm in the public engine.

## Not implemented this round

- **U5 (disagreement handling / independent verifier)**: a materially
  larger, distinct problem (reconciling two independently-produced
  verdicts for the *same* finding into one outcome) than family
  diversity (U4). Deferred as a clearly-named future increment -- see
  `validation/model_router_merge_readiness/latest-summary.md` section 5.
- Router telemetry (`router_decision_total`/`router_fallback_total`):
  the existing `patchfrog_provider_calls_total`/
  `patchfrog_candidates_by_tier_total` Prometheus counters are already
  defined in `patchfrog.ops.metrics` but not actually incremented
  anywhere in the review pipeline today (a pre-existing gap, confirmed,
  not introduced by this milestone). Adding new router-specific counters
  without fixing that underlying wiring gap would just add more unwired
  instrumentation -- deferred to a dedicated observability pass.
- No per-candidate dynamic model-tier escalation ("route this specific
  security-sensitive candidate to a stronger model") -- see the
  per-run-not-per-candidate rationale above. Quality + Cost Guard
  (`patchfrog.review.effort`) already controls which specialist roles
  run and critic strictness from exactly these kinds of signals; the
  router does not duplicate that logic.

## Version constants

`MODEL_ROUTER_VERSION = 1` (`patchfrog.routing.domain`) -- unchanged
across the pre-merge runtime-failover correction: PR #54 is still
unreleased/unmerged, so this correction defines the final, accepted v1
router contract (config-time selection semantics *and* runtime
failover) rather than amending an already-shipped one.

`QUALITY_COST_POLICY_VERSION` is **not** bumped, including for the
runtime-failover correction's own budget-gating addition -- audited
explicitly: the fallback hop reuses the exact same accounting
*mechanism* (the `max_total_input_tokens` reservation-then-reconcile
formula in `patchfrog.review.orchestration`) completely unmodified; a
new call site legitimately participating in an unchanged accounting
formula is not a change to the cost-policy *contract* itself (what a
budget field means, how it is computed, what counts as reserved vs.
actual). Nothing about `ReviewConfig`'s own fields or their effective
semantics changed.

`REVIEW_ENGINE_VERSION` is **not** bumped: normal review semantics (what
a specialist/critic does with its input, validation, dedup, confidence
aggregation) are unchanged; only *which* provider instance ends up
serving a given call differs, and only when the primary already failed.
