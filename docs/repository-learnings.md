# Repository Learnings Foundation

`patchfrog/repository_learnings/` extends
`patchfrog/historical_regression_memory/` (Milestone N) with a
deterministic answer to a narrower, stronger question: **has this
repository *repeatedly and independently* demonstrated a technical
pattern, and is the current PR re-entering it?**

**A single trusted historical event is never enough here.** Milestone
N can already act on one trusted `/patchfrog fixed`/`/patchfrog useful`
finding. Repository Learnings exists only to recognize *repetition*:
at least `MIN_SUPPORTING_EVENTS` (2) genuinely independent, trusted
occurrences on the exact same structural surface, from separate
historical review runs. One event, however strong, never produces a
`RepositoryLearning`.

## Reuses N's trust model verbatim -- never a second one

This package issues **zero SQL queries of its own**. It consumes
Milestone N's own already-fetched, already-point-in-time-correct
`HistoricalRegressionRecord`s
(`HistoricalRegressionReport.trusted_records_considered`) directly. A
future correction to N's eligibility/temporal rules automatically
fixes this package too -- there is no second, potentially-divergent
trust model to keep in sync.

## Independence: what counts as one occurrence

- **Distinct finding ids.** Two feedback events on the *same* finding
  (e.g. both `fixed` and `useful` replies) are one occurrence -- this
  already falls out of N's own query, which groups by `finding_id`.
- **Distinct historical review runs.** Two findings from the *same*
  historical review run are one occurrence, not two -- they were never
  independently re-observed over time. Grouping by
  `historical_review_run_id` and keeping the earliest record per run is
  this package's own, small addition on top of N's records.
- **Never a carried-forward/duplicate-publication copy.** N's query
  already reads from `ai_findings` (the validated/deduped output),
  never the full `ai_finding_proposals` audit trail -- a rejected or
  suppressed duplicate was never eligible to receive its own feedback.

## Pattern identity: structural, never semantic

`(repository_id, pattern_kind, anchor_file_path, anchor_qualified_name)`.
No NLP, no embeddings, no fuzzy text similarity anywhere. A record with
no `qualified_name` (e.g. a module-level finding) never participates --
falling back to file-only identity would reintroduce exactly the
over-broad match Milestone N's own correction round already ruled out
for `SAME_FILE`.

## Only one pattern kind is implemented in v1

`RepositoryLearningPatternKind.REPEATED_SAME_SURFACE_REGRESSION` --
repeated trusted findings on one exact `(file_path, qualified_name)`.

`REPEATED_COMPANION_REQUIREMENT`, `REPEATED_CONTRACT_CONSUMER_REQUIREMENT`,
and `REPEATED_TEST_REQUIREMENT` are named on the enum for forward
documentation only, and are **never constructed**. `review_runs` only
persists bounded aggregate *counts* per Intelligence layer (e.g.
`missing_companion_candidate_count`) -- never the identity of *which*
specific companion/consumer/test pair a historical finding concerned.
J/K/L/M's own candidate objects (`ExpectedCompanionChange`/
`ContractDelta`/`PotentialIntentGap`/`PotentialTestGap`) are computed
in-memory only, per review run, and never persisted as structured
rows. Reconstructing per-pair historical identity would require either
a new parallel history subsystem (out of scope -- this whole milestone
lineage deliberately avoids one) or inferring it from a finding's own
prose (explicitly forbidden). Both are deferred; see
`validation/repository_learnings/latest-summary.md` section 3 for the
full audit.

## Minimum support gate and activation time

`MIN_SUPPORTING_EVENTS = 2`, a hard floor, never configurable lower.
For a given anchor surface, one representative (earliest) record per
distinct historical review run is kept; a `RepositoryLearning` is only
constructed once the distinct-review-run count reaches the gate.

`activated_at` is the timestamp of the *N*-th (i.e.
`MIN_SUPPORTING_EVENTS`-th) earliest such run -- the moment the
review-run-distinct support count first crossed the threshold, never
the most recent event. A later, third+ independent occurrence extends
`support_count`/`last_observed_at` but never moves `activated_at`.

## No CANDIDATE/RETIRED lifecycle

A below-threshold pattern is never represented as an object at all --
there is no "candidate" state to track. Every constructed
`RepositoryLearning` is, by construction, `RepositoryLearningStatus.ACTIVE`.

**Invalidation falls out of live re-derivation, with no explicit
"retire" step**: nothing is ever persisted as a stateful learning row.
Every review run re-derives learnings fresh from N's own trust query.
If one of the supporting findings later receives a false-positive/
ignore event, N's own fail-closed `HAVING` clause drops it from the
trusted set for any `as_of` at or after that event -- the
review-run-distinct count for that surface naturally falls, and if it
drops below the gate, the `RepositoryLearning` is simply not
constructed on the next run. The corpus proves this directly
(`test_case_invalidation_falls_out_of_live_rederivation`).

## Current-PR application and dedup with N

An active learning applies to the current PR only when its exact
anchor `(file_path, qualified_name)` is **directly changed** by a
non-`TEST` `ChangeUnit` -- mirrors N's own `ChangeKind.TEST` exclusion
exactly, so a test-only PR that merely calls a learned-risky symbol
never triggers an application.

Every real application is constructed with
`status = RepositoryLearningApplicationStatus.UNSATISFIED`.
`SATISFIED`/`INSUFFICIENT_EVIDENCE` are reserved for a future pattern
kind with a real companion/consumer/test presence check to report
against -- `REPEATED_SAME_SURFACE_REGRESSION` has no companion target
to satisfy; the anchor being touched again *is* the entire signal.
`SATISFIED` is never published as praise/noise.

Whenever this package's own precondition holds for an anchor (>= 2
independent trusted findings), N's own reused query necessarily also
has a trusted record for that same surface, and N's own matching will
independently construct its own `SAME_SYMBOL` candidate for it. So in
practice a real application always has an existing N candidate to
enrich (`enriches_historical_regression`) -- this package never
publishes a second, competing warning. The check is not hard-coded to
assume this, however: it looks for a same-surface N candidate the same
defensive way N looks for a same-surface J/K/L match, falling back to
`stands_alone = True` if none is passed in.

## Repository isolation, renames/moves, security findings

Inherited directly from N: repository isolation is N's own
`repository_id` filter (this package adds no query of its own); a
renamed/moved symbol simply stops matching against the current pool
(no fallback attempted); `finding_category` is carried through
unchanged, so a repeated security finding needs no special-case code
path.

## Persistence

**No new table at all** -- one level further than N's own "zero new
history database": `RepositoryLearning` is never persisted as its own
row, always re-derived live from data N's query already reads. The
only new persisted state is five bounded summary columns on
`review_runs` (migration `0023_repository_learnings`):
`repository_learning_active_count`,
`repository_learning_application_count`,
`repository_learning_summary_rendered`,
`repository_learning_summary_text` -- the same cross-task-publication
pattern J/K/L/M/N established.

## Review pipeline integration

Computed last, right after Historical Regression Memory, consuming
`historical_regression_report.trusted_records_considered` and
`historical_regression_report.candidates` directly -- no second trust
query. A sixth optional `<repository_learning>` prompt section
(`REVIEW_PROMPT_VERSION` 8 -> 9), attached only to the exact candidate
matching a real, active learning application. `REVIEW_POLICY_VERSION`/
`REVIEW_ENGINE_VERSION`/every prior Intelligence package's own version
are **not** bumped. No new agent role, no new LLM calls anywhere in
this package (structurally proven -- no `LLMProvider` import).

## Change Story and conditional summary

`build_repository_learning_story_prefix` produces at most one bounded
sentence ("Repository learning: ... has repeatedly produced trusted
regressions across N independent reviews."), only when a real
current-PR application exists. The conditional `### Repository
learning` publication block uses the same eligibility bar. Neither
ever renders a percentage or gamified badge -- only the plain
independent-occurrence count.

## Limitations

- Only `REPEATED_SAME_SURFACE_REGRESSION` is implemented; companion/
  contract/test-requirement learning kinds are deferred (see above).
- Rename/move continuity is deferred, inherited from N.
- Cross-repository / cross-fork memory is deferred, inherited from N.
- A learning is heuristic evidence, not proof -- an application must
  still survive the existing reviewer/critic pipeline like any other
  piece of evidence before ever influencing a published finding.
