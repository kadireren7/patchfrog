# Quality + Cost Guard

`patchfrog/review/effort.py`, `patchfrog/review/effort_types.py`, and the
tier-aware parts of `patchfrog/review/orchestration.py` and
`patchfrog/review/service.py` introduce a deterministic layer that
decides *how much* review effort a candidate deserves, on top of Agent
Orchestration v1's cooperative Correctness/Security specialists (see
`docs/agent-orchestration.md`). This document explains that layer. It
does not introduce a new phase number.

**"The model may propose. PatchFrog decides what survives."** This
milestone extends that principle: **"PatchFrog decides how much model
work is justified."** No LLM ever decides whether another LLM gets
called -- every tiering decision is a pure function of already-known
structural/static signals, decided before any specialist provider call.

## Why: not every candidate deserves the same effort

Before this milestone, every reviewed candidate got the identical
treatment: both specialist roles, the same context/output budget
fraction, the same critic selectivity, the same retry allowance --
regardless of whether the candidate was a one-line change to a trivial
helper or a security-sensitive, cross-file, high-risk change. That is
safe but wasteful: routine candidates pay for verification effort they
don't need, while genuinely risky candidates get no *more* scrutiny than
routine ones. The Quality + Cost Guard fixes this without touching *what*
gets validated, dedup'd, or published -- only *how much execution effort*
a candidate is allowed to consume before publication is decided.

## The three tiers

`patchfrog.review.effort_types.ReviewEffortTier` -- exactly three
members, deliberately not more:

- **LIGHT** -- tiny, simple, no static findings, no security relevance,
  no adaptive context expansion, low structural complexity.
- **STANDARD** -- the normal case: some signal, but nothing that rises
  to LIGHT's "routine" or DEEP's "high-risk" bar.
- **DEEP** -- a real security signal, HIGH/CRITICAL static evidence, a
  high-risk static category (memory safety, resource management,
  concurrency, security), or multiple weaker structural signals
  corroborating each other.

Every reviewed candidate gets exactly one tier, decided by
`ReviewEffortPolicy` (composing `AgentSelectionPolicy` rather than
duplicating its security-relevance logic), **before any specialist
provider call**.

### Deterministic tier signals

`ReviewEffortPolicy.decide_provisional` looks only at the candidate and
its already-attached static findings:

- a static finding is present on the candidate at all;
- a static finding's severity is HIGH/CRITICAL;
- a static finding's category is security/memory-safety/resource-
  management/concurrency (the same high-risk category set already used
  by `patchfrog.context.adaptive`);
- the candidate's role selection includes Security for a *real* reason
  (`AgentSelectionReason.STATIC_SECURITY_CORROBORATION` or
  `SECURITY_SENSITIVE_NAMING` -- never the conservative fallback);
- the changed-symbol span is large, or many lines changed within the
  candidate.

None of these inspect comment text with fuzzy NLP, and no repository
path/name keyword is ever the *sole* decisive signal -- naming only
counts through the same explainable heuristic Agent Orchestration v1
already uses for Security selection. A real security/high-severity/
high-risk-category signal alone is decisive (DEEP); otherwise two or
more of the weaker structural signals together also reach DEEP
(corroboration, not any single weak signal); exactly one weak signal is
STANDARD; no signal at all is LIGHT. Every decision carries an explicit,
audited `ReviewEffortReason` tuple -- never "because."

## What tier controls -- and what it never controls

Tier may control: which specialist roles run, the context budget
fraction (and whether adaptive mode is even considered), critic
strictness, retry allowance, and the per-candidate output-token budget
split across roles.

Tier **never** controls provider, model, critic model, or credentials.
Those remain exclusively operator-controlled
(`patchfrog.review.runtime_config`, Milestone C) -- completely untouched
by this module. A repository or a tiering decision can never cause a
different provider/model to run.

### Role selection

- **LIGHT**: Correctness always required. Security runs only when a
  *real* signal exists (static security corroboration or
  security-sensitive naming) -- never suppressed when one does exist,
  since a real signal always forces DEEP anyway. When Security's only
  basis is the conservative fallback (no reliable signal either way),
  LIGHT drops it.
- **STANDARD/DEEP**: unchanged from Agent Orchestration v1's default --
  both roles always selected, regardless of *why* Security was included.

### Context budget

The context ceiling passed to the Context Engine
(`patchfrog.context.config.ContextConfig`) is derived from the
already-configured `max_input_tokens_per_candidate` ceiling, scaled by a
tier fraction (LIGHT 0.5, STANDARD/DEEP 1.0) -- **no tier can ever
exceed the operator/repository-configured ceiling, only reduce it.**
LIGHT also disables adaptive multi-hop expansion outright (fixed depth
1); STANDARD/DEEP keep today's adaptive default on. This is integration
*with* the existing Context Engine (`docs/context-engine.md`), not a
second Context Engine -- the same `ContextService`/`AdaptiveContextConfig`
machinery runs either way, just with a tier-scaled budget/mode.

### Critic strictness (`CriticExpectation`)

`patchfrog.review.critic_selection.CriticSelectionPolicy` gained a
`relaxed` parameter, used only for LIGHT/`OPTIONAL`:

- **OPTIONAL** (LIGHT): only the objectively-serious triggers
  (HIGH/CRITICAL severity, security category) still force critique;
  the cost-saving-adjacent catch-all rules (non-HIGH confidence,
  cross-role overlap, not statically corroborated) are relaxed away.
  LIGHT never skips critique for something genuinely risky -- it only
  spends fewer critic calls on proposals that are already
  unremarkable by every objective measure.
- **SELECTIVE** (STANDARD): exactly today's unchanged
  `CriticSelectionPolicy` behavior.
- **MANDATORY** (DEEP, or a candidate that escalated to DEEP): every
  valid, non-suppressed proposal is critiqued, bypassing the selective
  policy's skip rule entirely.

### Retry allowance

LIGHT is capped at `min(1, max_retries)` regardless of the configured
ceiling; STANDARD/DEEP use the full configured `max_retries`. No tier
may ever exceed the configured ceiling -- tiering can only narrow it.
Fatal provider errors are never retried at any tier (unchanged).

### Per-candidate output-token budget

`max_output_tokens_per_candidate` is a **shared, candidate-level**
ceiling, not a per-role one -- fixed by design so two concurrently-run
specialist roles can never together spend more than it. Each selected
role gets a deterministic, tier-fixed fraction of the ceiling
(`ReviewEffortDecision.per_role_output_token_fraction`): LIGHT 0.25 (one
role, total spend 0.25x), STANDARD 0.375 (two roles, total 0.75x), DEEP
0.5 (two roles, total 1.0x -- the full configured ceiling). This
fraction is deliberately **not** "candidate-level fraction divided by
role count" -- that formulation would let a single-role LIGHT
candidate's one role receive as much (or more) per-role budget as a
two-role STANDARD/DEEP candidate's role, inverting the intended
LIGHT < STANDARD <= DEEP ordering. Before this milestone, each role
independently received the *full* configured ceiling -- a real bug this
milestone fixes: two roles could together spend up to 2x the configured
per-candidate output budget.

## Two-stage decision, and bounded escalation

Tiering is necessarily two-stage, because one tier signal (adaptive
context expansion) only exists *after* context is built, while tier
itself controls the context budget used to build it:

1. **`decide_provisional`** -- before context is built, from only the
   candidate and its static findings. Determines the context
   budget/adaptive-mode policy used for context generation.
2. **`finalize`** -- after context is built, before any specialist
   provider call. May escalate (never de-escalate) the provisional tier
   by exactly one step, to DEEP, if and only if adaptive context
   expansion actually occurred (`ContextBundle.adaptive_metrics.occurred`)
   -- concrete depth-2 evidence, not a guess. Bounded to exactly one
   escalation per candidate, can never exceed the run's configured
   budget any more than DEEP itself already can, never recursive, never
   an LLM-based router. `ReviewEffortDecision.escalated`/
   `escalation_reason` record it for audit.

A **severity/security-triggered "escalation"** (e.g. a LIGHT candidate's
Correctness role happens to produce a HIGH-severity or security-category
proposal) is *not* a second escalation mechanism -- it is already
handled by `CriticSelectionPolicy`'s existing mandatory-critique rules
(which apply regardless of `relaxed`), so a LIGHT candidate's risky
finding still always gets verified before it can be accepted.

## Global run budget and reservation

`max_total_input_tokens` is a true run-level guard across **every**
provider call that consumes input tokens: both specialist roles' input,
*and* critic input (previously unguarded -- a real gap this milestone
closes). Reservation is atomic under the run's shared `asyncio.Lock`:
estimated cost is reserved before a call, then reconciled against the
provider's *actual* reported usage afterward under the same lock --
crediting back an overestimate (including a failed call's now-unused
reservation) and debiting further for an underestimate, never letting
the tracked total go negative. Reviewer reservation is atomic per
candidate (every selected role's combined estimate, or none -- never a
partially-reviewed candidate); critic reservation is atomic per
proposal, since each critique is an independent, separately-billed call.

**Budget exhaustion before a mandatory critic verification always
suppresses, never publishes.** If a proposal's required critique
(`CriticExpectation`, a severity/security mandatory-critique rule, or an
unresolved-contradiction group member) cannot get its critic call
reserved, that proposal is suppressed
(`ProposalStatus.SUPPRESSED_BUDGET`, suppression reason
`patchfrog.review.orchestration.CRITIC_BUDGET_EXHAUSTED`) rather than
published unverified -- "we already paid for the reviewer call" is never
a reason to skip required verification.

## Reviewer/critic/thinking-token accounting

`patchfrog.review.domain.TokenUsage` and `CriticVerdict` both gained a
`thinking_tokens` field, threaded from
`ProviderUsage.thinking_tokens` (already captured at the provider layer
but previously silently discarded when converted to the domain-layer
`TokenUsage`) -- 0 for providers/responses that don't report it, never
fabricated. `ReviewRunSummary`/`ReviewRunModel` gained
`reviewer_thinking_tokens`, `critic_thinking_tokens`,
`correctness_thinking_tokens`, `security_thinking_tokens`, `critic_calls`
(previously untracked as a distinct metric), and `retries_consumed`
(actual retry attempts consumed, not just the configured ceiling).
Latency continues to use provider-returned `latency_ms` per call;
concurrent-role latencies are never summed and reported as "wall-clock
review latency" -- they are per-call provider-work latencies, not the
review's overall duration (`ReviewRunSummary.duration_ms` remains the
real wall-clock figure, unchanged in meaning).

## Persistence and auditability

`ReviewCandidateModel` gained `effort_tier`, `effort_reasons` (JSON
array), `escalated`, `escalation_reason` -- nullable/default-safe, so
historical rows (predating this milestone, or a candidate skipped for
budget before any decision was even provisionally made) read back
honestly as "no tier decided" rather than a fabricated value.
`ReviewRunModel` gained `candidates_by_tier` (JSON object, tier value ->
count), `candidates_escalated`, `critic_calls`, `retries_consumed`, and
the thinking-token columns above. A single Alembic migration
(`migrations/versions/0016_quality_cost_guard.py`) adds all of these;
no Cloud billing/accounts schema was introduced.

## Operator hard caps vs. repository intent

Milestone C protected provider/model selection from repository control.
This milestone closes a related, previously-uncovered gap: a
repository's own `.patchfrog.yml` could otherwise request an arbitrarily
large `max_candidates`/`max_total_input_tokens`/
`max_output_tokens_per_candidate`/`max_concurrent_requests`/`max_retries`
and force the operator to spend accordingly. `patchfrog.config.settings.Settings`
gained five environment-only hard ceilings (never `.patchfrog.yml`-
controlled, exactly like provider/model credentials):

| Setting | Default |
| --- | --- |
| `PATCHFROG_MAX_REVIEW_CANDIDATES` | 100 |
| `PATCHFROG_MAX_TOTAL_INPUT_TOKENS` | 1,000,000 |
| `PATCHFROG_MAX_OUTPUT_TOKENS_PER_CANDIDATE` | 16,000 |
| `PATCHFROG_MAX_CONCURRENT_REVIEW_REQUESTS` | 16 |
| `PATCHFROG_MAX_REVIEW_RETRIES` | 5 |

`patchfrog.review.config_resolution.apply_operator_hard_caps` computes
`effective = min(repo_intent, operator_hard_cap)` independently per
field, applied by both the CLI and the production Celery task
immediately after resolving the repository's own config -- a repository
may voluntarily request *less* than these, never more. Defaults are set
generously above `ReviewConfig`'s own smaller defaults, so an
unconfigured self-hosted install behaves exactly as before this
milestone; these hard caps only bite when a repository's own
`.patchfrog.yml` asks for something unusually large.

`max_input_tokens_per_candidate` is deliberately **not** given its own
new operator ceiling (kept minimal -- see "avoid dozens of knobs"
below); `max_total_input_tokens` and `max_output_tokens_per_candidate`'s
caps are the primary defense against runaway context/output spend.

### Effective config identity

The returned (capped) `ReviewConfig` is exactly what canonical run
identity (`ReviewConfig.fingerprint()`) is computed from downstream -- a
repository asking for more than the operator allows is never silently
canonicalized as if it got what it asked for. This falls out "for free"
from `apply_operator_hard_caps` being applied *before* `fingerprint()` is
ever called, rather than needing a second, separate identity type.

## Config boundary: minimal repository-facing knobs

Per spec section 26, the Quality + Cost Guard's own tier
thresholds/fractions/policy constants live entirely as versioned engine
constants in `patchfrog.review.effort` (`_tier_semantics`,
`_LARGE_CHANGED_SYMBOL_LINES`, etc.) -- **no new repository-controlled
`.patchfrog.yml` field was added for tiering itself.** The only
repository-visible surface this milestone touches is
`max_output_tokens_per_candidate`'s *effective meaning* (shared ceiling,
not per-role) -- see "Versioning" below.

## Versioning

- `REVIEW_ENGINE_VERSION` 2 -> 3: every candidate's role selection,
  context budget, output-token budget, and retry allowance are now
  tier-driven rather than uniform -- a materially different call shape.
- `REVIEW_POLICY_VERSION` 3 -> 4: tier-driven bounded escalation and
  `CriticExpectation` change what can survive to a final finding (a
  LIGHT candidate's otherwise-unremarkable proposal now critiques less
  than before; a DEEP/escalated candidate's critique is now mandatory).
- `REVIEW_PROMPT_VERSION` unchanged at 3 -- no prompt text changed.
- `CONFIG_SCHEMA_VERSION` 3 -> 4: `max_output_tokens_per_candidate`'s
  effective repo-facing meaning changed materially (shared ceiling
  split across roles, not each role independently receiving the full
  value).
- `QUALITY_COST_POLICY_VERSION` (new, = 1): an independent version for
  the guard's own tiering policy, folded into `ReviewModelIdentity`'s
  fingerprint -- lets a future change to only the tiering
  thresholds/signals invalidate canonical-run reuse without needing a
  broader engine- or policy-version bump.

See `tests/unit/test_review_quality_cost_guard_versioning.py` for the
exact pinned values.

## Evaluation harness: guard vs. uniform baseline

`EvaluationRunner.run_case`/`_run_case_inner` gained
`use_quality_cost_guard: bool = True`. When `False`, the case runs
against `patchfrog.review.effort.uniform_baseline_decision` -- a fixed
decision reusing STANDARD's own semantics (both roles always selected,
existing adaptive context default, existing selective critic, full
configured retries) instead of a real, tiered `ReviewEffortPolicy`
decision. This is a **pipeline-preservation / cost-behavior comparison**,
never a claim about real-model quality: comparing guard-on against
guard-off with `FakeLLMProvider`/oracle infra shows whether call
counts/token counts/critic calls/tier distribution differ as expected
for identical fixtures, not whether real-model precision improved.
`CaseResult` and the suite-level `CandidateEfficiencyMetrics` both
expose `candidates_by_tier`, `candidates_escalated`, `critic_calls`,
`reviewer_thinking_tokens`, `critic_thinking_tokens`, and
`retries_consumed` for this comparison, alongside the pre-existing
per-role call/token breakdowns from Agent Orchestration v1.

## Failure semantics

- A LIGHT candidate whose sole (Correctness) role fails: candidate
  failed (same "all selected roles failed" rule as Agent Orchestration
  v1, just with a possibly-smaller selected-role set).
- A STANDARD/DEEP candidate with one specialist failing: not a candidate
  failure, exactly as before.
- Mandatory critic unavailable (budget exhausted, or the critic call
  itself transiently/fatally fails): the proposal is suppressed, never
  published unverified.
- Run budget exhausted before a candidate's reviewer reservation can be
  made: the whole candidate is skipped (`skipped_budget`), never
  partially started.
- An escalation that would require more budget than remains: the
  candidate proceeds at its (lower) provisional tier rather than
  blocking or retrying context generation -- escalation is opportunistic
  evidence-driven strengthening, never a hard requirement that can
  itself fail a candidate.
- Fatal provider errors are never retried, at any tier. Transient
  retries are bounded by the tier's `retry_limit`, itself bounded by the
  configured `max_retries`.

## Concurrency and estimation-error safety

Budget reservations (reviewer and critic) are guarded by the run's
single shared `asyncio.Lock` around the shared `budget_state` dict --
two concurrently-reviewed candidates can never both believe the same
remaining tokens are theirs. An underestimated prompt's actual usage
(reported only after the call completes) debits the tracked total
further on reconciliation, so a later candidate's reservation check sees
the corrected, larger total; an overestimate (or a failed call, whose
real usage is 0) credits the difference back. The tracked total is
never allowed to go negative.

## What this does not prove

Every test in this milestone uses `FakeLLMProvider`, an oracle-mode
reviewer, or pure unit-level policy logic -- **no live Anthropic/Gemini
calls were made**, and no test claims that DEEP tiering produces
*better* real-model findings than LIGHT would have, or that the guard
improves precision/recall against a live model. What is demonstrated is
narrower and load-bearing on its own: the guard's tier decisions are
deterministic and stable, role/context/critic/retry/output behavior
differs by tier exactly as designed, global and per-candidate budgets
are respected (including under concurrency and estimation error),
required verification is never bypassed for cost reasons, and the
uniform-baseline ablation still reproduces pre-guard call shape for
comparison. Real-model quality claims would require live-provider
evaluation against a real Anthropic/Gemini account, explicitly out of
scope for this milestone.

## What did not change

- Severity taxonomy, evidence validation rules, dedup heuristics,
  contradiction detection, and specialist prompt content -- all
  untouched. This milestone is execution-quality/cost control, not a
  prompt-tuning sweep.
- The GitHub comment format -- unaffected; none of this milestone's new
  fields are user-facing.
- Repository-controlled provider/model selection -- still impossible.
- Incremental review (`patchfrog.review_memory`) -- candidate narrowing
  is untouched; a narrowed candidate set still goes through the same
  two-stage tiering as a full run.
