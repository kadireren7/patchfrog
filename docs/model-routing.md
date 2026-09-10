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

`MODEL_ROUTER_VERSION = 1` (`patchfrog.routing.domain`) -- the router's
own durable semantic contract (route-plan shape, reason-code meaning).
`QUALITY_COST_POLICY_VERSION` is **not** bumped: routing selects *which*
provider serves a role, never the tiering policy itself (roles selected,
context budget, critic strictness) -- that contract is materially
unchanged. `REVIEW_ENGINE_VERSION` is **not** bumped: normal review
semantics (what a specialist/critic does with its input) are unchanged;
only *which* provider instance receives the call differs.
