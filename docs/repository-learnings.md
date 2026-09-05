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

`(repository_id, pattern_kind, anchor_file_path, anchor_qualified_name, finding_category)`.
No NLP, no embeddings, no fuzzy text similarity anywhere. A record with
no `qualified_name` (e.g. a module-level finding) never participates --
falling back to file-only identity would reintroduce exactly the
over-broad match Milestone N's own correction round already ruled out
for `SAME_FILE`.

**`finding_category` is part of identity, not metadata.** Two trusted
findings on the exact same symbol but a genuinely different category
(e.g. a SECURITY constant-time-comparison finding and an unrelated
CORRECTNESS None-handling finding) are not necessarily one repeated
technical pattern -- no richer root-cause identity is persisted
anywhere this package can safely read, so category is the one
additional structural signal available to avoid silently combining
them. This is conservative by construction: it can only ever *split* a
would-be learning into two (or suppress it below the support
threshold), never invent a false one.

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

## Current-PR application: enrichment only, never a standalone O warning

**This package never re-derives current-PR relevance on its own.** An
external-review correction round found the original v1 shape
independently checked whether a learning's anchor was directly changed
(its own `ChangeUnit`/`ChangeKind.TEST` walk) and constructed a
standalone application whenever it was -- making this package a
*second*, independent historical-regression detector, exactly what it
must never be. Fixed: `derive_repository_learning_applications` takes
no `change_units` at all. It only enriches an existing Milestone N
`PotentialHistoricalRegression` candidate on the *exact* same surface,
using that candidate's own already-correct current-PR identity. A
learning whose surface has no current N candidate this run produces
**no application at all** -- `enriches_historical_regression` is
mandatory, never optional, and there is no `stands_alone` case in v1.

This also means every current-relevance rule N itself established (the
direct-change vs. affected-surface distinction, the `ChangeKind.TEST`
exclusion, N's own dedup ownership against J/K/L) is inherited for
free, with nothing duplicated here.

**A real application carries no `status` field at all.**
"This exact surface has repeatedly produced trusted findings" is
historical-pattern *evidence*, not an invariant the current PR can
satisfy or violate -- an earlier v1 shape wrongly modeled it as
`UNSATISFIED`, which reads as "the current PR fails a requirement." It
does not; the anchor being touched again *is* the entire signal.
`RepositoryLearningApplicationStatus` (`SATISFIED`/`UNSATISFIED`/
`INSUFFICIENT_EVIDENCE`) is kept on the domain module purely for
forward documentation -- reserved for a genuinely relational future
pattern kind (anchor -> required companion) with a real presence check
to evaluate -- and is never referenced by
`PotentialRepositoryLearningApplication` in v1.

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
only new persisted state is two bounded summary *count* columns on
`review_runs` (migration `0023_repository_learnings`):
`repository_learning_active_count`,
`repository_learning_application_count`. No rendered-text column at
all -- see "Change Story, and no separate summary block" below for why.

## Review pipeline integration

Computed last, right after Historical Regression Memory, consuming
`historical_regression_report.trusted_records_considered` and
`historical_regression_report.candidates` directly -- no second trust
query. A sixth optional `<repository_learning>` prompt section
(`REVIEW_PROMPT_VERSION` 8 -> 9), attached only to the exact candidate
matching a real, active learning application -- bounded evidence
(pattern kind, category, support count, first/last trusted timestamps)
attached only to the candidate N's own report already justified.
`REVIEW_POLICY_VERSION`/`REVIEW_ENGINE_VERSION`/every prior
Intelligence package's own version are **not** bumped. No new agent
role, no new LLM calls anywhere in this package (structurally proven
-- no `LLMProvider` import).

## Change Story, and no separate summary block

`build_repository_learning_story_prefix` produces at most one bounded
sentence ("Repository history: ... has produced trusted findings
across N independent reviews."), folded into the same combined Change
Story text every other Intelligence package's own prefix joins, only
when a real current-PR application exists. Never phrased as an
invariant violation -- no "unsatisfied," no "missing," no "violates."

**Unlike every prior Intelligence package, there is no standalone
`### Repository learning` publication block in v1 at all** (no
`summary.py` module in this package). An external-review correction
round found that because a real application always enriches an
existing N candidate on the exact same surface, a second top-level
section would render immediately next to N's own `### Historical
context` block about that very surface -- saying, in effect, the same
thing twice. This package's entire user-facing footprint in v1 is
therefore the single Change Story addendum above, plus bounded
per-candidate prompt evidence, plus count-only telemetry.

## Limitations

- Only `REPEATED_SAME_SURFACE_REGRESSION` is implemented; companion/
  contract/test-requirement learning kinds are deferred (see above).
- Rename/move continuity is deferred, inherited from N.
- Cross-repository / cross-fork memory is deferred, inherited from N.
- Because a real application requires an existing N candidate, a
  learning whose surface N does not currently flag (e.g. a symbol only
  affected, not directly changed, in a way N's own hierarchy doesn't
  match) never produces an application either -- this package is
  strictly narrower than N's own current-relevance reach by design.
- A learning is heuristic evidence, not proof -- an application must
  still survive the existing reviewer/critic pipeline like any other
  piece of evidence before ever influencing a published finding.
