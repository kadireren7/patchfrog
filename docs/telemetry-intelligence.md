# Evaluation & Telemetry Intelligence

`patchfrog/telemetry/` (domain, collector, aggregation, reporting) plus
targeted additions to `patchfrog/evaluation/` (identity, regression,
runner) and `patchfrog/ops/metrics.py`. This document explains the
telemetry layer and the evaluation-comparison changes built alongside
it. It does not introduce a new phase number, a Cloud dashboard, or a
new provider/agent.

**"The model may propose. PatchFrog decides what survives."** This
milestone adds one more sentence: **"PatchFrog can explain what it did,
and how well it did it, without ever re-exposing what it read."**

## Telemetry vs. feedback vs. benchmark ground truth

Three distinct concepts. Never conflated, anywhere in this codebase:

| | What it is | Where it lives | Canonical for |
|---|---|---|---|
| **Operational telemetry** | What PatchFrog actually did -- structured metadata/provenance/outcomes | `patchfrog/telemetry/` | Nothing about correctness. It is a record of *process*, not *quality*. |
| **User feedback** | Real-world reactions/replies/commands from developers | `patchfrog/feedback/` | Nothing, on its own. Noisy evidence -- a thumbs-down is never proof a finding was wrong, a resolved thread is never proof it was right, and *missing* feedback is never proof of approval. |
| **Benchmark ground truth** | Human-authored expected findings in evaluation fixtures | `patchfrog/evaluation/` | TP/FP/precision/recall. The only source of a benchmark score. |

Concretely:

- An LLM critic's `reject` verdict is never treated as a benchmark false
  positive -- it is a critic *decision*, reported as exactly that.
- A user's `/patchfrog false-positive` command produces
  `user_reported_false_positive` in telemetry/feedback -- never a bare
  "false positive," which would misleadingly suggest benchmark-grade
  certainty.
- Telemetry aggregates may show benchmark metrics and feedback metrics
  *side by side* in the same report; they are never combined into one
  score (no blended "quality index," no F1 computed from a mix of the
  two).

## Privacy model

Telemetry collects structured metadata/provenance/outcomes. It is **not**
a second warehouse of raw private customer code.

By construction (enforced by the type system -- the dataclasses in
`patchfrog/telemetry/domain.py` simply have no field for this), telemetry
never persists or exports:

- raw source files or full diff contents
- raw prompts (system or user)
- raw context snippets (`ContextItem.content`)
- quoted evidence text (`ReviewEvidence.quoted_text`)
- API response bodies
- secret-like environment values

Telemetry instead references existing persisted review/context/feedback
entities by their stable ids (`review_run_id`, `candidate_id`,
`proposal_id`, `finding_id`, `context_bundle_id`, ...). A telemetry
consumer that needs the actual finding text already has a canonical
place to get it: the existing `ai_findings` table, via
`patchfrog.review.queries`.

What telemetry *does* carry: provider/model identity, agent role,
effort tier, category/severity/confidence, validation/critic/suppression
outcomes, token/call/latency counts, adaptive-context metadata, and file
path + line range (documented here as repository metadata that a future
external/aggregated export may choose to drop -- nothing in this
milestone exports telemetry outside the operator's own database/CLI).

`tests/integration/test_telemetry_collector.py::test_json_export_contains_no_secret_or_content_sentinel`
plants sentinel secret-shaped strings in a real persisted proposal's
`message`/`evidence`/`reasoning_summary` and in a real context item's
`content`, then asserts neither ever appears in the exported JSON.
`tests/unit/test_telemetry_reporting.py::test_no_telemetry_dataclass_carries_a_forbidden_content_field`
enforces the same guarantee structurally, over every telemetry
dataclass, independent of any one test's fixture data.

## Finding lifecycle

Every AI proposal (`patchfrog.persistence.models.review.AIFindingProposalModel`)
gets exactly one terminal `patchfrog.telemetry.domain.FindingLifecycleOutcome`:

```
proposed                    -- non-terminal; never produced for a real persisted row
validation_rejected
critic_rejected
critic_downgraded           -- ACCEPTED status + a critic DOWNGRADE verdict
suppressed_duplicate        -- cross-role dedup *and* post-run AI/AI dedup share this status
suppressed_contradiction
suppressed_budget
below_confidence_threshold
accepted_final
```

`patchfrog.telemetry.domain.classify_lifecycle_outcome` computes this
from two already-typed, already-persisted values -- the proposal's
`ProposalStatus` and (for `ACCEPTED`) its critic verdict's
`CriticDecision` -- never from parsing `validation_detail` or
`reasoning_summary` prose. This is why migration `0017_telemetry_intelligence`
had to add `ai_finding_proposals.validation_outcome`: before this
milestone, a proposal's own `ValidationOutcome` (see below) was computed
but never persisted, only a free-text detail string was -- and
telemetry must never infer a machine-classified outcome from prose.

`suppressed_duplicate` deliberately covers two different mechanisms
(cross-role duplicate suppression in `patchfrog.review.agents.cross_role`,
and post-run AI/AI dedup in `patchfrog.review.dedup`) under one status,
because that is what PatchFrog itself persists today -- telemetry
reports what was actually recorded, never a distinction it would have to
guess at from a validation-detail string.

## Validation, critic, and suppression metrics

- **Validation outcomes** (`patchfrog.review.domain.ValidationOutcome`:
  `VALID`, `HALLUCINATED_LOCATION`, `HALLUCINATED_EVIDENCE`,
  `INVALID_TAXONOMY`, `OUT_OF_SCOPE`, `INCOMPLETE_ANALYSIS`) are broken
  down by role, tier, and category via
  `patchfrog.telemetry.aggregation.compute_validation_outcomes_by_role/_by_tier/_by_category`.
  These are never called "false positives" -- only benchmark ground
  truth or explicit user feedback can establish that.
- **Critic outcomes** (`accept`/`reject`/`downgrade`) are broken down by
  role/tier/severity via `compute_critic_decisions_by_role/_by_tier/_by_severity`.
  A critic `reject` is never a benchmark false positive.
- **Suppression reasons** are never merged into one number --
  `compute_lifecycle_outcome_counts` gives `suppressed_duplicate`,
  `suppressed_contradiction`, and `suppressed_budget` as three separate
  keys, always.

## Role and tier breakdowns

`patchfrog.telemetry.aggregation.compute_role_funnel` /
`compute_tier_funnel` report, per role/tier: `proposed`,
`validation_valid`, `validation_rejected`, `critic_rejected`,
`accepted_final` -- the minimum funnel every slice needs (spec-required
shape). A higher accepted-findings rate at one tier is never interpreted
as "better quality" here -- these are counts, not a verdict.

## Quality funnel

`patchfrog.telemetry.aggregation.compute_quality_funnel` reports:
`candidates -> proposals -> validation_valid -> accepted_final ->
published_findings -> feedback_bearing_findings`, plus a `drop_off` dict
covering every non-accepted outcome individually. `drop_off` values plus
`accepted_final` always sum to exactly `proposals` -- no proposal is
ever double-counted or silently dropped from the funnel.

`published_findings` in v1 means "persisted to `ai_findings`" -- the
terminal PatchFrog-internal disposition. Whether a finding is actually
*commented on GitHub* is a further-filtered downstream step
(`patchfrog/publishing/`, severity threshold + `max_inline_comments`)
this milestone does not add run-level telemetry for yet; the funnel's
final stage is honestly labeled `published_findings`, not
`github_commented_findings`.

## Adaptive context metrics

`patchfrog.telemetry.domain.ContextTelemetry` carries
`adaptive_attempted`/`adaptive_occurred`/`adaptive_reasons`/
`adaptive_direction`/`depth_2_candidate_count`/`depth_2_selected_count`/
`depth_2_tokens` -- read directly off `ContextBundleModel`, never
recomputed. `compute_adaptive_context_summary` aggregates these across a
snapshot's bundles. `compute_context_effectiveness` compares
proposals/finals/validation-rejection-rate on candidates whose context
was adaptively expanded against candidates whose wasn't -- purely
observational language throughout ("candidates with expanded context
had N finals"), never a causal-improvement claim.

## Quality + Cost Guard metrics

`patchfrog.telemetry.aggregation.compute_tier_distribution` reports
candidates by `ReviewEffortTier` and the escalated count, directly from
`CandidateTelemetry.effort_tier`/`escalated`/`escalation_reason` (which
in turn come straight from `review_candidates.effort_tier`/`escalated`/
`escalation_reason`, added in migration `0016_quality_cost_guard`).
Reviewer/critic call counts, tokens, and thinking tokens are broken out
by role via `ProviderTelemetry.reviewer_by_role` (one entry per
`AgentRole`).

## Provider usage and latency semantics

Reviewer and critic usage are two clearly separate sections of
`ProviderTelemetry` -- never summed into one ambiguous "provider usage"
number.

**Wall-clock duration vs. provider-work latency aggregate -- never
conflated.** `ReviewTelemetrySnapshot.duration_ms` is the run's real
wall-clock time. `ProviderTelemetry.reviewer_latency_ms_aggregate` /
`critic_latency_ms_aggregate` are *sums* of every individual provider
call's own reported latency -- since specialist roles run concurrently
(`asyncio.gather` in `patchfrog.review.orchestration`) and critic
verifications run concurrently too, these aggregates can and do
legitimately exceed the run's wall-clock duration. Treating either
aggregate as "how long the run took" would be a real, actively-tested-for
bug (`tests/integration/test_telemetry_collector.py::test_reviewer_and_critic_latency_aggregates_are_never_confused_with_wall_clock`).

Reviewer per-role call latency was **genuinely missing** before this
milestone -- `patchfrog.review.provider.ProviderResult.latency_ms` was
computed by every provider but discarded in
`AgentOrchestrator._call_role`. Migration `0017_telemetry_intelligence`
adds `review_runs.reviewer_latency_ms` (a run-level sum); critic latency
needed no new column, since `critic_verdicts.latency_ms` (added by
migration `0006_ai_reviewer`) already lets the collector sum it directly
per run via a batched query. Per-specialist-role reviewer *call counts*
were also computed in memory (`ReviewRunSummary.calls_by_role`) but
never persisted -- `review_runs.calls_by_role` (new in this migration)
closes that gap the same way `candidates_by_tier` already does.

Cost is never converted to dollars. The primary cost unit is tokens
(input/output/thinking), provider calls, and retries -- no Anthropic/
Gemini pricing table exists anywhere in this codebase, and none is
added here. A future milestone that wants a `CostEstimator` should
introduce it as a versioned, operator-configured component; this
milestone deliberately does not.

## Feedback integration and denominators

`patchfrog.telemetry.domain.FeedbackTelemetry` carries one entry per
*published* finding (`has_feedback=False` when none exists -- "unknown,"
never "confirmed correct"). `patchfrog.telemetry.aggregation.compute_feedback_coverage`
computes:

- `coverage_rate` = feedback-bearing findings / published findings
- `useful_rate`, `user_reported_false_positive_rate`, `fixed_rate` --
  each denominated by **feedback-bearing findings only**, never by all
  published findings. A finding with no feedback contributes to neither
  the numerator nor the denominator of any rate except `coverage_rate`
  itself.

`user_reported_false_positive_rate` is named that way deliberately (not
"false_positive_rate") -- it is real-world user signal, never benchmark
ground truth. This module never computes a "global false-positive rate"
from feedback alone.

### Finding-scoped vs. review-scoped feedback

`patchfrog.feedback` attribution is deliberately best-effort (see
`patchfrog.feedback.attribution`): a raw signal -- a reaction, a reply,
an explicit `/patchfrog <token>` command, a PR-lifecycle event -- may
have `FeedbackEvent.finding_id = None` when it can't be resolved to one
exact published finding, or structurally never has one at all (a
`PR_MERGED`/`PR_CLOSED` event is about the whole PR, never a single
finding). The raw event is still retained for audit either way; nothing
in `patchfrog.feedback` ever discards it.

Telemetry mirrors that distinction explicitly via `FeedbackScope`
(`FINDING` / `REVIEW`), never collapsing one into the other:

- `ReviewTelemetrySnapshot.feedback: tuple[FeedbackTelemetry, ...]` --
  one entry per *published finding*, always `scope=FeedbackScope.FINDING`.
  Unchanged by this section.
- `ReviewTelemetrySnapshot.review_feedback: tuple[ReviewFeedbackEventTelemetry, ...]`
  -- one entry per feedback event that could not be (or structurally
  never could be) attributed to one finding, always
  `scope=FeedbackScope.REVIEW`. This dataclass has no `finding_id` field
  at all -- there is nothing to misattribute even by accident.

`compute_review_feedback_summary` aggregates these into
`review_feedback_event_count`/`review_feedback_by_event_type`/
`review_feedback_by_signal` -- **plain counts only**, never collapsed
into one fabricated truth label. Two conflicting review-scoped events
(e.g. one `/patchfrog useful` and one `/patchfrog false-positive`, both
unattributable to the same finding) are both retained and both counted,
not merged into a single "verdict."

**Isolation is structural, not just a convention**: `compute_feedback_coverage`
takes `Sequence[FeedbackTelemetry]` -- calling it with
`snapshot.feedback` (the only correct call) is mechanically incapable of
including review-scoped events, since `ReviewFeedbackEventTelemetry` is
a different, incompatible type. Every finding-level rate
(`coverage_rate`, `useful_rate`, `user_reported_false_positive_rate`,
`fixed_rate`) is therefore unaffected by however much review-scoped
feedback exists on the same run.

Privacy: `ReviewFeedbackEventTelemetry` is safe by the same construction
as everything else in this package -- `patchfrog.feedback.sync` never
writes a reply/comment body into `raw_signal`/`normalized_signal`/
`metadata` (a reply event's `raw_signal` is always the empty string; its
`metadata` holds only `{"reply_comment_id": "<id>"}`), and this
dataclass doesn't even expose `raw_signal`/`metadata` -- only the
already-safe `normalized_signal`.

## Benchmark quality metrics -- unchanged

`patchfrog.evaluation.metrics` (TP/FP/missed/duplicate/unsupported/
out_of_scope, precision/recall/F1, clean-case pass rate, severity
behavior, security-quality metrics) is untouched by this milestone.
Matcher semantics (`patchfrog.evaluation.matcher`) are unchanged.

## Evaluation cost/efficiency reporting

`patchfrog.evaluation.metrics.CandidateEfficiencyMetrics` (pre-existing,
Milestone F) already reports `candidates_by_tier`, `candidates_escalated`,
`critic_calls`, `reviewer_thinking_tokens`/`critic_thinking_tokens`,
`retries_consumed`, and `provider_calls`/`*_tokens_per_tp`. This
milestone extends `patchfrog.evaluation.regression.compare` with three
new cost checks (`cost_provider_calls`, `cost_input_tokens`,
`cost_critic_calls`), always computed and reported, but **report-only
by default** -- `RegressionThresholds.max_provider_calls_increase_pct`/
`max_input_tokens_increase_pct`/`max_critic_calls_increase_pct` all
default to `None`, meaning the check always appears in a comparison's
output with its delta, but never fails the run unless a caller
explicitly configures a real percentage. A cost regression is not
automatically a quality regression, so CI must never be silently gated
on a threshold nobody configured.

## Evaluation identity changes

`patchfrog.evaluation.domain.EvaluationIdentity` gained four fields:

- `context_engine_version` (`patchfrog.context.config.CONTEXT_ENGINE_VERSION`)
- `quality_cost_policy_version` (`patchfrog.review.config.QUALITY_COST_POLICY_VERSION`)
- `quality_cost_guard_enabled` (`True` for every real review and the
  default evaluation path; `False` only for the evaluation harness's
  fixed "uniform baseline" ablation)
- `context_config_identity` (the `ContextConfig.fingerprint()` of
  whatever `context_config_override` a run used, or the literal string
  `"default"` when none was supplied)

All four now participate in `patchfrog.evaluation.regression.identity_compatible`'s
must-match set. Before this milestone, a guard-on run and a "uniform
baseline" ablation run -- or a fixed-depth-1 run and an adaptive-context
run -- could be silently compared as if they were the same baseline,
because nothing in `EvaluationIdentity` distinguished them. This was a
real gap, not hypothetical: `patchfrog.cli._run_context_ablation`'s three
existing context-ablation variants (`normal`/`target-only`/
`no-extra-context`) each ran with a different `ContextConfig` override
but all three built their reported `EvaluationIdentity` with the
*same* (default) `context_config_override=None`, so their identities
were indistinguishable even though the underlying runs weren't -- fixed
in this milestone alongside the new field.

`EVALUATION_ENGINE_VERSION` is bumped 1 -> 2 (the identity shape and
regression/reporting logic changed materially).
`EVALUATION_BENCHMARK_VERSION` is unchanged (no fixture ground truth
changed). `REVIEW_ENGINE_VERSION`/`REVIEW_POLICY_VERSION`/
`REVIEW_PROMPT_VERSION`/`CONFIG_SCHEMA_VERSION`/`CONTEXT_ENGINE_VERSION`/
`QUALITY_COST_POLICY_VERSION` are all unchanged -- this milestone
observes and reports on review/context behavior, it does not change it.

## Guard-vs-uniform and fixed-vs-adaptive comparisons

The actual pipeline-preservation/cost-behavior comparisons already exist
from Milestone F and Milestone E respectively
(`tests/integration/test_evaluation_runner_end_to_end.py::test_quality_cost_guard_ablation_changes_call_shape_for_identical_fixture`
and `::test_evaluation_supports_fixed_and_adaptive_context_ablation`,
both Fake/oracle-only, no live provider). This milestone's contribution
is making the two arms' `EvaluationIdentity` genuinely distinguishable
(see above) -- verified directly in
`tests/unit/test_evaluation_telemetry_identity.py` (`test_guard_on_vs_uniform_identity_differs`,
`test_fixed_depth_1_vs_adaptive_identity_differs`), so a report
comparing the two arms can never silently claim they're the same
baseline.

## Telemetry collector API

```python
async def collect_review_telemetry(
    session: AsyncSession, *, review_run_id: uuid.UUID
) -> ReviewTelemetrySnapshot | None
```

The one canonical entry point. Queries existing persisted data only
(candidates, proposals, findings, critic verdicts, context bundles,
feedback events -- a small, fixed number of queries, batched by id list
where a second table is joined; never one query per row). Returns
`None` for an unknown run id, never raises for a plain not-found.
Deterministic: identical DB state produces an identical snapshot
(`==` on the frozen dataclass). Never mutates review state -- calling it
twice, or calling it for a bogus id, never changes anything in
`review_runs`/`review_candidates`/`ai_finding_proposals`.

`patchfrog.telemetry.aggregation.aggregate_snapshots` sums many
snapshots (one run, one repository's runs, or an arbitrary set of run
ids the caller collected) into a `TelemetryAggregate`. Deliberately just
totals -- every breakdown (role/tier/category/validation-outcome/
critic-decision/funnel) is a separate pure function over the snapshots'
own tuples, so aggregating over an arbitrary run-id set never needs a
second, database-specific analytics query path.

## Failure semantics

Telemetry collection must never break review publishing, and a
completed review's own state must never depend on telemetry succeeding.
`collect_review_telemetry` issues read-only `SELECT`s exclusively (no
`session.commit()`, no model mutation) and is never called from the hot
review path (`patchfrog.review.service`/`patchfrog.review.orchestration`)
-- it is only ever invoked on demand, from the CLI or a future reporting
consumer, against an already-completed run.
`tests/integration/test_telemetry_collector.py::test_collector_failure_never_affects_a_completed_review`
proves a not-found lookup returns `None` without raising and without
touching a real, already-persisted run's counts.

## JSON export and CLI

```
patchfrog telemetry review <run-id>                    # human-readable text summary
patchfrog telemetry review <run-id> --format json       # full JSON to stdout
patchfrog telemetry review <run-id> --output snapshot.json  # also write JSON to a file
```

`TELEMETRY_SCHEMA_VERSION` is included in every export
(`patchfrog.telemetry.reporting.snapshot_to_dict`). Every `StrEnum`
member serializes as its plain string value (never a Python repr);
every `UUID` serializes as its string form. `patchfrog.telemetry.reporting.write_json`/
`read_json` mirror `patchfrog.evaluation.reporting`'s exact file-artifact
pattern (sorted keys, trailing newline).

Bumped 1 -> 2 for Change Intelligence Foundation (`docs/change-intelligence.md`):
`ReviewTelemetrySnapshot` gained the `change_intelligence` field
(`ChangeIntelligenceTelemetry` -- counts only, never Change Story/Change
Map prose), and `snapshot_to_dict` exports every dataclass field via
`dataclasses.asdict`, so this is a real exported-JSON-shape change even
though it's purely additive. Historical rows (persisted before this
milestone, or any run whose `mark_succeeded` call predates passing
`change_intelligence`) export `change_intelligence` with explicit
zero/default values -- never a fabricated Change Story or count. See
`tests/unit/test_telemetry_reporting.py::test_json_export_shape_explicitly_contains_change_intelligence_object`
and `tests/integration/test_telemetry_collector.py::test_historical_row_without_change_intelligence_exports_defaults_under_schema_2`.

## Persistence strategy

Telemetry is derived from existing normalized persistence wherever
possible -- there is no `telemetry_events` table, and this milestone
adds no such thing. Exactly three columns were added
(`migrations/versions/0017_telemetry_intelligence.py`), each because the
underlying data was genuinely missing, not merely inconvenient to
re-derive:

| Column | Why it couldn't be derived |
|---|---|
| `review_runs.reviewer_latency_ms` | Per-role call latency (`ProviderResult.latency_ms`) was computed by every provider call but discarded before persistence -- no source of truth existed anywhere. |
| `review_runs.calls_by_role` | Per-role call counts existed only in the in-memory `ReviewRunSummary` for the lifetime of one request; a later read of a reused/reconstructed run summary silently lost them. |
| `ai_finding_proposals.validation_outcome` | The typed `ValidationOutcome` a proposal's own validation produced was computed but only a free-text `validation_detail` string was ever persisted -- and telemetry must never infer a machine-classified outcome from prose. |

All three are nullable-safe / zero-default for historical rows (see
below) and required no backfill.

## Historical compatibility

The collector tolerates rows predating Agent Orchestration, Adaptive
Context, and Quality + Cost Guard:

- `ReviewCandidateModel.effort_tier is None` -> `CandidateTelemetry.effort_tier = None` (never a fabricated tier).
- `ContextBundleModel.adaptive_expansion_attempted = False` (the column's own default) -> `ContextTelemetry.adaptive_attempted = False`, `adaptive_occurred = False` -- both a "never attempted" bundle and a genuinely-old bundle read back identically, since that is the honest answer either way.
- `AIFindingProposalModel.validation_outcome is None` -> `FindingLifecycleTelemetry.validation_outcome = None` for any row predating migration `0017`.
- `AgentRole is None` on a proposal/finding predating Agent Orchestration -> grouped under an explicit `"unknown"` key in every role breakdown, never silently dropped or merged into one of the two real roles.

`tests/integration/test_telemetry_collector.py::test_historical_nullable_rows_are_supported`
covers this directly.

## Query-bound behavior

`collect_review_telemetry` issues a small, fixed number of queries
(run, candidates, proposals, findings, verdicts batched by proposal-id
list, context bundles batched by bundle-id list, feedback events for the
run) regardless of how many candidates/proposals/findings the run has --
`tests/integration/test_telemetry_collector.py::test_collector_query_count_does_not_scale_linearly_with_proposal_count`
asserts the real SQLAlchemy-emitted query count stays under a fixed
bound for an 8-proposal/3-candidate/2-bundle/2-feedback-event scenario.

## Prometheus / operational metrics

Three new low-cardinality counters (`patchfrog/ops/metrics.py`):
`patchfrog_candidates_by_tier_total{tier}`,
`patchfrog_candidates_skipped_budget_total`,
`patchfrog_critic_calls_total`. See
[Metrics](operations.md#metrics) in `docs/operations.md` for the full
registry and the cardinality rule these follow: no repository name, PR
number, candidate id, finding id, file path, or model/prompt content
ever becomes a Prometheus label. Detailed per-run/per-candidate/
per-finding analysis belongs in the telemetry snapshot, not a metric
label.

## What telemetry does NOT prove

- It does not prove a finding was correct or useful -- only benchmark
  ground truth (in an evaluation run) or explicit human feedback
  establishes that, and even feedback is noisy evidence, not proof.
- It does not prove PatchFrog's quality improved between two engine
  versions -- only a compatible evaluation-run comparison (matching
  `EvaluationIdentity`) against benchmark ground truth does that.
- Adaptive-context and tier-effectiveness comparisons
  (`compute_context_effectiveness`, tier funnels) are observational
  correlations over whatever candidates happened to run, never causal
  claims and never a substitute for a real evaluation-suite comparison.
- A critic `reject` does not prove the rejected proposal was wrong.
- Missing feedback does not prove a finding was accepted, ignored, or
  correct -- it proves nothing at all about that finding.
- None of this milestone's real dogfood/validation runs used a live
  Anthropic/Gemini call -- see the final-report section of the PR this
  document ships with for exactly which existing `FakeLLMProvider`/
  oracle-scripted runs were used instead.
