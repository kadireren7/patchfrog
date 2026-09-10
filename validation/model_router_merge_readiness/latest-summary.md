# Milestone U+V — Model Router + Merge Readiness: Audit

Base: `main` @ `90bb543e78200e3130df3462742fcd9b25df0f30`. Written before any
U/V implementation, per this repo's workflow discipline (`CLAUDE.md`
"Development workflow"). Combines the roadmap's existing **U — OpenAI
Provider + Model Router** and **V — Merge Readiness / Decision Layer**
entries (`docs/roadmap.md` "Next — Review Engine") with the caller's more
detailed specification for this round.

## 1. Provider / review orchestration (current state)

- `patchfrog/review/provider.py`: `LLMProvider` Protocol — `identity:
  ProviderIdentity` + `async generate_structured(request:
  ProviderRequest) -> ProviderResult`. `ProviderRequest(system_prompt,
  user_prompt, json_schema, schema_name, max_output_tokens)`.
  `ProviderResult(raw_json, usage, latency_ms, stop_reason)`. Errors:
  `ProviderError` → `ProviderTransientError` (rate limit/overload/dropped
  connection, retry-safe) / `ProviderFatalError` (auth, malformed
  request, schema-parse failure, refusal — never retried). This is the
  exact, sole contract every provider (real or fake) implements; U1 adds
  a third implementation, never a parallel interface.
- `patchfrog/review/providers/anthropic_provider.py` /
  `gemini_provider.py`: one `generate_structured` call each, dict-based
  JSON Schema passed straight through (`output_format`/
  `response_json_schema` respectively) — never a Pydantic-model
  translation layer, since PatchFrog's own schemas
  (`patchfrog/review/schemas.py`) are already plain JSON Schema dicts.
  `patchfrog/review/providers/fake.py`: `FakeLLMProvider` (records
  `.calls`, scripted responses/exceptions) + `route_by_schema_name` for
  per-role dispatch in tests — reused for the OpenAI/router corpus below.
- Provider selection today is **single-provider, whole-deployment**:
  `Settings.review_provider` (`PATCHFROG_REVIEW_PROVIDER`, default
  `"anthropic"`) + `review_model`/`review_critic_model`
  (`patchfrog/config/settings.py`). `ReviewRuntimeConfig`
  (`patchfrog/review/runtime_config.py`) resolves this from `Settings`
  only, never `.patchfrog.yml`. `provider_factory.build_reviewer_provider`/
  `build_critic_provider` construct one concrete adapter from
  `SUPPORTED_PROVIDERS = ("anthropic", "gemini")`.
- **Key structural finding**: `AgentOrchestrator.__init__`
  (`patchfrog/review/orchestration.py`) already takes
  `reviewer_providers: Mapping[AgentRole, LLMProvider]` — a per-role
  provider mapping is *already* the real shape used at dispatch
  (`self._reviewer_providers[role]`). `PullRequestReviewService`
  currently populates it as `{CORRECTNESS: self._reviewer_provider,
  SECURITY: self._reviewer_provider}` (`patchfrog/review/service.py:
  ~1010`) — same single instance for both roles, once per review run.
  **The Model Router's job is to produce this mapping (plus a critic
  provider) from policy — the mechanism it plugs into already exists and
  needs no orchestration change.**
- `AgentOrchestrator` (and therefore the provider mapping) is constructed
  **once per review run**, before the per-candidate loop, not once per
  candidate. Effort tiering (`patchfrog/review/effort.py`,
  `ReviewEffortTier` = `LIGHT`/`STANDARD`/`DEEP`,
  `QUALITY_COST_POLICY_VERSION = 4`) *is* decided per-candidate
  (`decide_provisional`/`finalize`/`escalate_for_high_risk_proposal`,
  from candidate + static findings + post-proposal risk), and controls
  which roles run, output-token budget fraction, and critic strictness —
  **never provider/model/credentials** (explicit in its own docstring).
  This is a real architectural constraint for the router: **varying
  provider per candidate would require moving orchestrator construction
  inside the per-candidate loop**, which the current token-budget
  reservation locking is not designed around, and is not something
  T's own docstrings or the existing `ReviewModelIdentity` persistence
  shape (below) anticipate. Scope decision (§7).
- `ReviewModelIdentity` (`patchfrog/review/config.py`) is the *run-level*
  provenance record already persisted onto `ReviewRunModel`
  (`reviewer_provider`, `reviewer_model`, `critic_provider`,
  `critic_model`, plus `prompt_version`/`policy_version`/
  `engine_version`/`quality_cost_policy_version`) — plain `String`
  columns, not FK/enum. `CONFIG_SCHEMA_VERSION = 4` versions
  `ReviewConfig` (repository-controlled behavior only).
  `OPERATOR_ONLY_REVIEW_FIELDS = ("provider", "model", "critic_model",
  "request_timeout_seconds")` is the existing, explicit rejection list a
  `.patchfrog.yml` trying to set these hits — reused verbatim for any new
  router-related field name that could collide.
- CLI/runtime wiring (`patchfrog/cli.py`): three call sites all go through
  `resolve_review_runtime_config(settings)` →
  `build_reviewer_provider`/`build_critic_provider`. No DI container;
  provider construction is inline per-command. Router integration point:
  replace these two calls with one `ModelRouter.route(...)` call
  returning a `ReviewRoutePlan`, whose `reviewer_providers`/
  `critic_provider` are then passed into `PullRequestReviewService`.

## 2. Evidence persisted per-finding vs. run-level (Merge Readiness inputs)

- `AIFindingModel` (`patchfrog/persistence/models/review.py`): severity,
  confidence, category, `corroborated_by_static: bool` +
  `static_finding_ids: str (json)`. **No lifecycle/resolved field on the
  model itself** — resolution must be derived from
  `FeedbackAssessmentModel` (below) or a same-head `FixAttempt`, never
  read off `AIFindingModel` directly.
- Publication state: `ReviewPublicationModel` (one per
  review_run+mode) → `ReviewPublicationCommentModel` (one row per finding
  fed to the planner), `disposition: PublicationDisposition`
  (inline/summary-only/omitted) + `github_comment_id`. This is the
  per-finding "was this actually shown to a human" record.
- `ReviewRunStatus` (`patchfrog/review/domain.py`): `RUNNING`,
  `SUCCEEDED`, `PARTIAL`, `FAILED`. **`PARTIAL` is exactly the "review ran
  but a required step failed" state Merge Readiness needs** — it already
  distinguishes "found nothing because the run finished clean" from
  "found nothing because it didn't finish." No new completeness state
  machine is needed; `ReviewRunStatus` already models it.
- Stale-head: `PullRequestModel.head_sha` (updated on each push
  ingestion) vs. a `ReviewRunModel.commit_sha` — a plain equality check
  is the correct staleness primitive here. T's `verify_ancestor_with_diff`
  solves a materially different problem (proving descendance across
  arbitrary SHAs for fix verification) and is not reused for readiness.
- Feedback/dismissal: `FeedbackAssessmentModel`/`FeedbackAssessment`
  (`patchfrog/feedback/domain.py`) — `resolution_signal: ResolutionState`
  (`OPEN`/`CLOSED`/`UNKNOWN`), recomputable per
  `(finding_id, assessment_version)`. `FEEDBACK_ASSESSMENT_VERSION = 1`.
  **This is the authoritative "is a human treating this finding as still
  open" signal** — reused, not reinvented, for whether an accepted
  finding still counts as "unresolved."
- J–R Intelligence: confirmed unchanged from T's own audit
  (`validation/agent_handoff/latest-summary.md` §1.2) — every layer
  (Change/Contract/Intent/Test/Historical/Repository-Learnings/
  Trajectory/Cross-PR/Cross-Repo) persists only **run-level aggregate
  columns** on `ReviewRunModel` (counts + summaries), none FK'd to a
  specific `ai_findings` row. Only static analysis
  (`static_finding_ids`) and Executable Verification (ephemeral, never
  persisted — T §1.3, unchanged) are finding-addressable. **This directly
  constrains Merge Readiness: J–R evidence can only ever be
  contextual/`HUMAN_REVIEW_REQUIRED`-grade signal at the review-run
  level, never finding-specific `BLOCKED` evidence** — matches Part AI's
  own instruction precisely.
- `FixAttempt` (`patchfrog/fix_verification/domain.py`): confirmed
  unchanged — `FixAttemptStatus` = `PENDING`/`VERIFYING`/`FIXED`/
  `STILL_PRESENT`/`INCONCLUSIVE`/`STALE`/`ERROR` (7 total), unique on
  `(handoff_id, candidate_fix_commit_sha)`.

## 3. GitHub publication surface / MCP / docs conventions

- `patchfrog/publishing/` (not `publication/`): `patchfrog/github/client.py`
  has exactly one write-capable review method,
  `create_pull_request_review` (`POST
  /repos/{owner}/{repo}/pulls/{number}/reviews`). **No Check Run / Commit
  Status method exists anywhere in the codebase.** `GitHubReviewEvent`
  (`patchfrog/domain/github_review.py`) has exactly one member,
  `COMMENT` — APPROVE/REQUEST_CHANGES are deliberately excluded already,
  with an explicit prior docstring: "never renders a merge-affecting
  verdict; that is an explicit later-phase product/policy decision." No
  GitHub App manifest file exists in-repo; nothing today requests
  `checks:write`/`statuses:write`. **Adding a Check Run/Status would be a
  genuinely new permission scope** — Part AO says not to request this
  unless clearly necessary, and the existing safe path (one PR Review
  object, `COMMENT` event, per run) is sufficient for a v1 readiness
  summary. Decision: extend the existing single-Review summary body,
  request no new GitHub permission.
- No edit/update-existing-comment concept exists (the marker/reconciliation
  logic in `patchfrog/publishing/marker.py` only recognizes a *crashed
  mid-publish retry*, not a later re-review) — each new run posts a fresh
  Review. The readiness line is added to that same fresh Review's summary
  body, not a separately-maintained comment.
- `patchfrog/mcp/server.py`: 4 existing tools confirmed unchanged
  (`list_findings`, `get_finding_handoff`, `start_fix_attempt`,
  `get_fix_attempt`), each a nested `async def` inside `_register_tools`
  decorated `@self.mcp.tool()`. A 5th, read-only `get_merge_readiness`
  tool slots into the identical pattern.
- `docs/roadmap.md` already scopes this exactly: **U1 OpenAI provider
  adapter, U2 model capability registry, U3 deterministic routing, U4
  reviewer/critic model-family diversity, U5 disagreement handling /
  independent verifier**, and V's goal paragraph already states
  READY/BLOCKED/HUMAN_REVIEW_REQUIRED with "never an arbitrary numeric
  risk score" verbatim. `docs/product-boundary.md` verbatim: *"if it
  determines how PatchFrog reviews code, it belongs in this
  source-available engine; if it operates PatchFrog as a hosted SaaS
  business, it belongs in the private Cloud control plane"* — and
  explicitly lists "Managed provider/model routing" as Cloud-only
  ("PatchFrog Cloud manages provider selection and routing internally...
  without requiring any change to a user's repository configuration").
  The generic routing *algorithm* (this milestone) stays in the public
  engine; a future Cloud's *production policy* (weights, experiments,
  provider health fleet) does not exist here and is not implemented.
- `docs/deployment.md`: today documents exactly one provider variable set
  (`PATCHFROG_REVIEW_PROVIDER` singular). No multi-provider/routing
  concept exists yet in operator docs — this milestone introduces it.

## 4. OpenAI SDK research (current, as of 2026-09)

- Package: `openai` on PyPI, latest `3.11.0`, license **Apache-2.0**,
  requires Python ≥3.10 (compatible with this repo's ≥3.12 floor).
  Pinned `"openai>=3.11,<4.0"` — the same major-version-locked pattern as
  `anthropic>=0.68,<1.0` / `google-genai>=2.0,<3.0`.
- The Chat Completions endpoint is legacy; the **Responses API**
  (`client.responses.create`/`client.responses.parse`) is OpenAI's
  current recommended path and the one used here — the Assistants API is
  separately sunset, not used by PatchFrog anyway.
- Structured output: PatchFrog's schemas are plain JSON Schema dicts
  (not Pydantic models), so the adapter uses
  `client.responses.create(..., text={"format": {"type": "json_schema",
  "name": schema_name, "schema": json_schema, "strict": True}})` — the
  raw-schema path, exactly parallel to Gemini's `response_json_schema`
  and Anthropic's `output_format`, never a Pydantic-model translation
  layer PatchFrog doesn't otherwise use.
- Refusal: a `response.output[i].content[j].type == "refusal"` content
  item, distinct from normal text — mapped to `ProviderFatalError`
  (never retried), the same semantic as Gemini's
  `_REFUSAL_FINISH_REASONS` check and Anthropic's `stop_reason ==
  "refusal"`.
- Incomplete/truncated: `response.status == "incomplete"` with
  `response.incomplete_details.reason` (`"max_output_tokens"` or
  `"content_filter"`) — `"content_filter"` maps to `ProviderFatalError`
  (a safety refusal in different clothing); `"max_output_tokens"` is left
  to fail JSON/schema parsing naturally downstream, exactly like a
  truncated Anthropic/Gemini response.
- Exceptions (`openai.*`, all inherit `APIError`): `APIConnectionError`,
  `APITimeoutError`, `RateLimitError` (429), `AuthenticationError` (401),
  `PermissionDeniedError` (403), `BadRequestError` (400),
  `UnprocessableEntityError` (422), `InternalServerError` (5xx),
  `APIStatusError` (catch-all non-2xx). Mapping mirrors the existing
  Anthropic/Gemini adapters exactly: `RateLimitError` +
  `APIConnectionError` + `APITimeoutError` + `InternalServerError` →
  `ProviderTransientError`; `AuthenticationError` +
  `PermissionDeniedError` + `BadRequestError` +
  `UnprocessableEntityError` → `ProviderFatalError`.
  `client.with_options(timeout=..., max_retries=0)` disables the SDK's
  own retry (default 2) per-call, the same reasoning as
  `_NO_SDK_RETRY`/`_SDK_MAX_RETRIES = 0` in the existing two adapters —
  PatchFrog's own orchestrator applies bounded retry on top; SDK-level
  retry would silently compound delay.
- `AsyncOpenAI` (async client, same surface as sync) is the one actually
  used, matching the other two adapters' `async def generate_structured`.

## 5. Scope decisions for this round

1. **U5 ("disagreement handling / independent verifier") is explicitly
   deferred**, not implemented this round. It is a materially larger,
   distinct problem (reconciling two independently-produced verdicts for
   the *same* finding into one outcome) that the caller's own detailed
   spec for this round never actually describes a mechanism for — only
   family *diversity* (critic from a different family when available) is
   specified (Part L), which is U4, not U5. Implementing invented
   disagreement-resolution semantics without a concrete spec would be
   exactly the kind of undirected scope creep this repo's workflow
   discipline exists to prevent. Left as a clearly-named future
   increment.
2. **Model Router decides provider selection once per review run, not
   per candidate** (§1 above) — the current orchestration and
   provenance-persistence shape (`AgentOrchestrator` constructed once per
   run; `ReviewModelIdentity` a run-level record) does not support
   per-candidate provider identity without a materially larger, separate
   change (moving orchestrator construction inside the per-candidate
   loop, plus a schema change to persist per-finding provider
   provenance). The router still uses real, candidate-derived signals
   (see Part J) — but aggregated once, up front, across the run's
   candidate set, exactly the same granularity `ReviewModelIdentity`
   already persists. Documented honestly as a v1 limitation, not
   silently narrowed.
3. **Merge Readiness reuses `PullRequestModel.head_sha` equality for
   staleness**, not T's `verify_ancestor_with_diff` — a different,
   simpler problem (exact match, not descendance proof).
4. **No Check Run / Commit Status surface** — extends the existing single
   PR Review summary body instead, to avoid requesting a new GitHub App
   permission scope for presentation alone (Part AO's own instruction).
5. **No new `.patchfrog.yml` readiness policy surface** — Part AR
   explicitly permits deferring this ("If no safe policy surface is
   needed in V: do not add one yet"); v1 ships hard platform defaults.
6. J–R Intelligence evidence is used only for `HUMAN_REVIEW_REQUIRED`
   escalation reasoning (contextual), never `BLOCKED` — per §2 and Part
   AI, since none of it is finding-specific persisted evidence.

Implementation follows below this audit; see `docs/model-routing.md` and
`docs/merge-readiness.md` for the shipped architecture, and the
"Final report" delivered at the end of this milestone's turn for gate
results, version-bump justification, and the controlled corpus results.

## 6. Implementation-time addendum: GitHub publication surface deferred

While implementing V's "one concise readiness surface" (Part AN),
inspection of `patchfrog/publishing/service.py`/`planner.py` found a
real architectural tension not visible from the audit alone:
`PublicationPlanner.build_plan` is deliberately documented as **pure --
"No network calls, no database session, no LLM call"** — every input is
already-computed plain values (see `change_story`/`intent_coverage_summary`
etc., all pre-computed and persisted onto `ReviewRunModel` *during the
review itself*, never fetched live at publish time). `MergeReadinessService.evaluate`
requires a live `AsyncSession` (queries `PullRequestModel`/`ReviewRunModel`/
`AIFindingModel`/`FeedbackAssessmentModel`/`FixAttemptModel` fresh), and
`PullRequestReviewService.publish`'s own stale-head detection
(`current_head_sha` fetched live from GitHub, compared inside
`build_plan` itself, producing `ReviewPublicationStatus.STALE`) happens
*after* the point where a readiness computation would need to run --
computing readiness earlier risks evaluating it against a head the
planner is about to independently discover is already stale.

Given the live, already-tested, production-critical nature of this
exact pipeline (idempotent publish attempts, crash-recovery
reconciliation, exactly-once GitHub writes), wiring readiness into it
without dedicated design attention for this interaction would be
exactly the kind of unverified change to a hard-to-reverse path this
project's own engineering discipline warns against. **Decision: the
GitHub PR Review summary body is not extended with a readiness line
this round.** Milestone V's "one concise readiness surface" for v1 is
the read-only MCP `get_merge_readiness` tool (T2's existing pattern,
exact-head-bound by construction, already implemented and tested below)
-- not a gap, a deliberate, documented scope boundary. A future focused
follow-up should specifically design how readiness's live-DB-query need
composes with the planner's pure-function contract and its own
independent staleness detection, rather than being folded in here under
this milestone's already-large scope.

## 7. Pre-merge correction: runtime provider execution failover + OpenAI refusal-priority fix

Post-implementation review of PR #54 found the Model Router satisfied
only *configuration-time* provider-selection fallback (preferred
provider uncredentialed -> a different provider selected before any
call). There was no actual bounded runtime failover when the *selected*
primary provider failed during a real review call (timeout, rate limit,
transient server error, or a schema-invalid response) -- exactly the
capability the original milestone's own test matrix (primary timeout,
malformed primary response, fallback allowed/denied by budget, no
infinite fallback) required and the initial implementation had not yet
built. A second, independent bug was found in the OpenAI adapter's
refusal handling.

### 7.1 Runtime failover architecture

`patchfrog.review.orchestration.AgentOrchestrator` gained optional
`reviewer_fallback_providers`/`critic_fallback` constructor parameters
(`None` by default -- pre-correction behavior exactly unchanged when
omitted). `_call_role` (reviewer roles) and `_critique_one` (critic) each
now internally: attempt the primary via the existing `call_with_retry`
bounded-retry policy, unchanged; on `ProviderTransientError` (retries
exhausted) or `ResponseSchemaError` (validation moved *inside* these
methods specifically so a malformed response is eligible for the same
treatment -- previously validation happened in a separate step the
caller performed afterward, where no fallback concept could reach it);
attempt exactly one fallback call (`max_retries=0`, no retry of its own,
no chain, never back to the primary); `ProviderFatalError` is never
caught, so it propagates immediately without ever attempting a fallback
(explicit policy: an auth/config/refusal failure is not assumed safe to
retry against a different vendor by default in v1).

`patchfrog.routing.domain.ReviewRoutePlan` gained
`reviewer_fallback_providers`/`reviewer_runtime_fallback_family`/
`critic_fallback_provider`/`critic_runtime_fallback_family`/
`runtime_fallback_permitted`, and its pre-existing `fallback_used` field
was renamed `config_fallback_used` to make the two concepts
unambiguous at every call site (a breaking rename, safe since PR #54 is
still unreleased). `ModelRouter.route` computes the runtime-fallback
family the same way for both reviewer and critic: prefer the explicit
`PATCHFROG_ROUTER_FALLBACK_PROVIDER` if it names a *different*,
credentialed provider; otherwise any other configured provider; `None`
if no distinct alternative exists (matches single-provider deployments,
which continue to have no runtime fallback available, exactly as they
have no diversity available). Both reviewer and critic fallback reuse
the *same* setting as configuration-time fallback -- one operator-facing
backup-provider concept, two independent trigger points, never a second
config surface.

### 7.2 Cost-budget integration

The fallback hop's actual token usage (never an estimate) flows through
the exact same reservation-then-reconcile formula every role's call
already used, unmodified -- proven directly in
`test_fallback_actual_usage_is_accounted_in_the_run_wide_budget`. A
dedicated, read-only gate (denies the fallback attempt outright when
`budget_state["used_input_tokens"] + role_estimate > max_total_input_tokens`)
was added specifically so "budget exhausted -> fallback denied" is real
and testable, without a separate top-up reservation that could desync
the existing, carefully-tuned reconciliation math elsewhere in this
module -- proven in `test_fallback_denied_when_budget_has_no_room_for_it`.
`QUALITY_COST_POLICY_VERSION` is not bumped for this (see
`docs/model-routing.md`'s own version-constants section for the full
reasoning): the accounting *formula* itself is unchanged, only a new
call site legitimately participates in it.

### 7.3 Review completeness / Merge Readiness integration

`PullRequestReviewService`'s own aggregation of per-candidate
`CandidateOrchestrationResult.failed` into the run's `ReviewRunStatus`
was not touched by this correction -- a candidate whose every selected
role fails (even after an attempted, also-failing fallback) is marked
`failed=True` exactly as before, so the run is never reported
`SUCCEEDED` when review work didn't actually complete. Since
`MergeReadinessService` only ever reads `ReviewRunStatus` and never
anything about *how* a run reached that status, this integration was
correct by construction -- verified end-to-end (not merely asserted) via
`tests/integration/test_runtime_failover_merge_readiness_integration.py`,
which runs a real `PullRequestReviewService.review_local` call through a
real git fixture repository with every specialist call failing and no
fallback configured, and confirms `MergeReadinessService.evaluate`
resolves to `HUMAN_REVIEW_REQUIRED`/`REVIEW_INCOMPLETE` -- never
`READY` -- for that exact head. A second case in the same file proves
the positive path: primary failure + a *successful* fallback completes
the review normally, and readiness then evaluates exactly as if the
primary had never failed.

### 7.4 OpenAI refusal-priority bug (Blocker 2)

`patchfrog/review/providers/openai_provider.py`'s `_extract_text`
returned the *first* content block it found across every message in
`response.output`, and its own docstring claimed refusal-first priority
that the implementation didn't actually provide: a response shaped
`[output_text, refusal]` (same message or a later one) would have
returned the text before ever inspecting the refusal block after it. No
live testing had exercised this exact ordering, so the gap went
unnoticed until directed re-audit.

Fixed by splitting extraction into two full passes over
`response.output`, never returning early on the first content block
seen: `_any_refusal` scans every content block of every message item
before returning anything, so a refusal anywhere in the response wins
regardless of its position; text extraction then uses
`response.output_text`, the *official* SDK helper (not a hand-rolled
walk) -- OpenAI's own documentation states it is "not safe to assume
that the model's text output is present at `output[0].content[0].text`"
and that the helper "aggregates all text outputs from the model into a
single string," which is exactly the multiple-`output_text`-block case
this correction also had to account for, already correctly solved by
the SDK rather than reimplemented here.

### 7.5 New/updated tests

`tests/unit/test_review_openai_provider_contract.py` gained 5 cases
(29 total, up from 24): `output_text` then `refusal` in the same
message, `refusal` then `output_text` in the same message, `output_text`
in an earlier message with `refusal` only in a later one (the exact
bug reproduction), multiple `output_text` blocks concatenated via the
official helper, and malformed JSON text passed through unmodified
(never parsed/validated by the adapter itself, exactly like
Anthropic/Gemini).

`tests/integration/test_runtime_provider_failover.py` (new, 19 cases):
primary transient-error classes (generic/timeout-shaped/rate-limit-
shaped/5xx-shaped) all trigger fallback identically; fallback succeeds
vs. fallback also fails (role honestly marked failed, exactly one
fallback attempt made); no fallback configured / fallback mapping
missing this role behave identically to each other; fallback never
attempts a second cross-provider hop and never loops back to the
primary; retry-plus-fallback total calls remain bounded and observable
via `.calls`; fallback's actual usage is accounted in the run-wide
budget; fallback denied when the budget has no room; malformed
structured output is eligible for fallback (never retried against the
same non-compliant primary first); `ProviderFatalError` (including a
refusal-shaped one) is never eligible for fallback; reviewer fallback
and critic family diversity are proven independent; executed-provider
provenance reports the fallback family when used and the primary family
otherwise.

`tests/integration/test_runtime_failover_merge_readiness_integration.py`
(new, 2 cases) -- see 7.3 above.

`tests/unit/test_model_router.py`/`tests/unit/test_review_service_route_plan.py`
updated for the `fallback_used` -> `config_fallback_used` rename (28
router tests unaffected in count, all still pass).

### 7.6 Full-suite result

New test files added by this correction: 19 cases
(`test_runtime_provider_failover.py`) + 2 cases
(`test_runtime_failover_merge_readiness_integration.py`) + 5 new OpenAI
refusal-ordering cases within the existing contract-test file (24 -> 29).
Zero regressions across the pre-correction 2321-test baseline -- see the
final report for the exact fresh full-suite count.
