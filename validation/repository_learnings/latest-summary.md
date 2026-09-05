# Milestone O: Repository Learnings Foundation -- Pre-Implementation Audit

Written before any code. Answers the spec's own audit questions before
implementation begins, exactly like J/K/L/M/N's own audits.

## 1. What is a "repeated, independent, trusted" pattern, precisely?

A *repository learning* is the claim "this repository has, on at least
`MIN_SUPPORTING_EVENTS` (2) separate, independent occasions, received
trusted developer feedback confirming a technical issue on this exact
surface" -- distinct from Milestone N (Historical Regression Memory),
which can act on a **single** trusted event. O's entire distinguishing
value is the *repetition* requirement: one trusted event is evidence a
mistake happened once; two or more, from genuinely separate review
runs, is evidence the repository has a standing structural tendency
worth surfacing as reusable context.

"Independent" is defined precisely, reusing Phase 9/Milestone N's own
identity primitives -- never invented fresh:

- **Distinct finding ids.** Two feedback events on the *same*
  `finding_id` (e.g. a developer replying both `/patchfrog fixed` and
  later `/patchfrog useful` on one finding, or the same finding
  receiving two `fixed` replies) are **one** occurrence, not two. This
  already falls out of Milestone N's own
  `fetch_trusted_historical_records` query for free: it groups by
  `finding_id` via `GROUP BY FeedbackEventModel.finding_id`, so the
  result set already contains at most one row per finding regardless
  of how many raw feedback events targeted it.
- **Distinct review runs.** Two findings from the *same* historical
  review run (e.g. two findings on the same symbol proposed and
  trusted within one run) must not satisfy independence -- they were
  never independently re-observed over time, just two proposals from
  one pass. O's own aggregation step (not N's, which doesn't need it)
  must additionally group by `historical_review_run_id` and count
  **distinct review-run ids**, not raw record count, before comparing
  against `MIN_SUPPORTING_EVENTS`.
- **Never counting a carried-forward/duplicate-publication copy.**
  Milestone N's underlying query already reads from `ai_findings`
  (the validated/deduped output), never `ai_finding_proposals` (the
  full, unfiltered audit trail that can contain rejected/suppressed
  duplicates) -- so a re-published or duplicate-suppressed proposal
  was never eligible to receive its own independent feedback event in
  the first place. Reusing N's query for O's own evidence fetch
  inherits this guarantee for free; nothing new needs to be built to
  satisfy it.

## 2. Can O reuse Milestone N's trust model as-is?

Yes, exactly, per the spec's own explicit instruction ("do NOT build a
second trust model"). `patchfrog.historical_regression_memory.queries.fetch_trusted_historical_records`
already returns the eligibility-filtered, point-in-time-correct
(`occurred_at <= as_of`), repository-scoped set of trusted
`HistoricalRegressionRecord`s -- one row per qualifying finding, each
carrying `historical_review_run_id`, `source_file_path`,
`source_qualified_name`, `finding_category`, `evidence_strength`,
`observed_at` (the point-in-time-computed `trusted_at`). O's own query
layer is therefore almost entirely reuse: call N's function, then
group/aggregate the *already-correct* records in Python. No second SQL
trust query, no second temporal model, no second fail-closed
false-positive/ignore check -- all of that logic lives in exactly one
place (N's query), and a future correction to it automatically fixes
O too.

## 3. Which pattern kinds are safely reconstructable from *already-persisted* data?

This is the central scoping question, and it determines most of the
rest of this design. Checked directly against
`patchfrog/persistence/models/review.py`:

- `ReviewRunModel` persists only **bounded aggregate counts** per
  Intelligence layer (`missing_companion_candidate_count`,
  `contract_delta_count`, `impacted_consumer_count`,
  `stale_consumer_candidate_count`, `intent_gap_candidate_count`,
  `test_gap_candidate_count`, etc.) -- never the *identity* of which
  specific (anchor, companion) or (anchor, consumer) or (anchor, test)
  pair was flagged. J/K/L/M's own `ExpectedCompanionChange`/
  `ContractDelta`/`PotentialIntentGap`/`PotentialTestGap` objects are
  computed **in-memory only**, per review run, and never persisted as
  structured rows.
- `AIFindingModel` persists a single finding's own
  `file_path`/`category`/`title`/`candidate_id` (which joins to
  `ReviewCandidateModel.qualified_name`) -- a single-surface identity,
  exactly what Milestone N's own `HistoricalRegressionRecord` already
  captures. It carries **no** structured link to "the companion this
  finding was about," "the contract consumer this finding concerned,"
  or "the test this finding said was missing."

Consequence: **`REPEATED_SAME_SURFACE_REGRESSION`** (repeated trusted
findings on one exact `(file_path, qualified_name)` surface) is the
only pattern kind safely reconstructable from what is already
persisted -- it needs nothing beyond N's own record shape, grouped and
counted. **`REPEATED_COMPANION_REQUIREMENT`**,
**`REPEATED_CONTRACT_CONSUMER_REQUIREMENT`**, and
**`REPEATED_TEST_REQUIREMENT`** would each require either (a) a new
persistence subsystem recording the exact per-pair companion/consumer/
test identity at the time of every historical review run (out of
scope -- a new parallel history store, exactly what this whole
milestone lineage has deliberately avoided), or (b) inferring the
missing companion's identity from a finding's own prose (explicitly
forbidden: "never NLP/embedding/semantic similarity over old finding
text"). Both are unsafe for v1. **Decision: v1 implements
`REPEATED_SAME_SURFACE_REGRESSION` only; the other three pattern kinds
are named on the enum for forward documentation and explicitly
deferred**, mirroring N's own precedent of keeping `SAME_FILE` on its
enum while never constructing it. Per spec section 40's own allowance
("if companion/contract/test pattern reconstruction is not supported
by current persistence: mark those cases DEFERRED and implement
same-surface learning only"), this is an anticipated, acceptable v1
scope, not a shortfall.

## 4. Pattern identity

Structural, never semantic: `(repository_id, pattern_kind, anchor_file_path, anchor_qualified_name)`.
For the only implemented kind, `anchor_qualified_name` is never `None`
(a record with no qualified name -- e.g. a module-level/file-scoped
finding -- cannot participate: there is no stable non-file identity to
repeat against, and falling back to file-only would reintroduce
exactly the over-broad `SAME_FILE` failure mode N's own correction
round already ruled out). `learning_id` is a deterministic string hash
of that tuple -- never a random UUID (must be reproducible across
independent computations of the same underlying evidence, and never
persisted as its own row in v1 -- see section 8 below).

## 5. Minimum support gate and activation time

`MIN_SUPPORTING_EVENTS = 2` (hard-coded floor, never configurable
lower). For a given anchor, group N's trusted records by
`historical_review_run_id`, taking the **earliest** `observed_at`
within each review run as that run's own representative timestamp (a
single review run could in principle produce more than one trusted
finding on the very same surface; only one of them can count toward
independence). Sort those per-run representative timestamps ascending.
If the count of distinct review runs is `< MIN_SUPPORTING_EVENTS`, no
`RepositoryLearning` is constructed at all -- there is no below-
threshold "candidate" object in this design (see section 6 below).

`activated_at` = the timestamp of the *N*-th (i.e.
`MIN_SUPPORTING_EVENTS`-th) earliest per-run timestamp -- literally
"the moment the review-run-distinct support count first reached the
threshold," which is exactly the spec's own "`max(trusted_at)` of the
minimum support set" (the minimum set that *just* satisfies the gate
is the earliest `MIN_SUPPORTING_EVENTS` runs; its max is the last one
needed to cross the line).

## 6. Do we need a CANDIDATE/ACTIVE/RETIRED lifecycle, or does "derive fresh every time" make this unnecessary?

The spec explicitly allows simplifying ("if simpler architecture
works: candidate vs active may be enough... only introduce states if
lifecycle is truly necessary"). Given the "derive live, never persist
a stateful row" architecture (section 8), a below-threshold pattern
is never constructed as an object in the first place -- there is
nothing to call CANDIDATE. Every `RepositoryLearning` this package
ever constructs is, by construction, active. **Decision: implement
`RepositoryLearningStatus` with a single meaningful value, `ACTIVE`**
(kept as an enum, not a bare boolean, purely so a future lifecycle
state has somewhere to go) -- and do not implement `RETIRED` as its
own lifecycle: point 7 below shows invalidation/retirement already
falls out for free from re-deriving live every run, so a separate
"mark retired" mechanism would be dead code.

## 7. Invalidation / retirement -- does it fall out naturally?

Yes. Because `RepositoryLearning` is never persisted as its own
stateful row -- always recomputed, per review run, from
`fetch_trusted_historical_records(as_of=...)` -- if one of the two (or
more) supporting findings later receives a `false_positive`/`ignore`
event, N's own query's `HAVING` clause
(`false_positive_count = 0 AND ignore_count = 0`) drops that finding
from the trusted set entirely for any `as_of` at or after that new
event. O's own aggregation naturally sees one fewer independent
review run for that anchor and, if the remaining count now falls below
`MIN_SUPPORTING_EVENTS`, simply does not construct the
`RepositoryLearning` any more. No explicit invalidation code is
needed -- it is a direct, provable consequence of live re-derivation
plus N's own already-correct trust query. A corpus case proves this
(section "Invalidation" below).

## 8. Persistence decision

**Do not persist `RepositoryLearning` rows.** Mirrors N's own "zero
new history database" precedent, one level further: O needs *no* new
table at all, not even a lightweight one, because everything is
derived live from data N's own query already reads. The only new
persisted state is five bounded summary columns on `review_runs`
(counts + a rendered flag + rendered text), exactly matching every
prior milestone's own cross-task-publication pattern -- a new Alembic
migration, no new table.

## 9. Current-PR relevance / application status

For each active learning, check whether its anchor
`(file_path, qualified_name)` is *directly changed* in the current
review (present in some `ChangeUnit.changed_candidates`, non-TEST
kind, mirroring N's own pool-building discipline exactly -- reusing
N's `ChangeKind.TEST` exclusion so a test-only PR that merely calls a
learned-risky symbol never triggers a learning application, exactly
the failure mode both M and N's own corpora already caught and fixed
once).

`PotentialRepositoryLearningApplication.status`: the spec wants
SATISFIED/UNSATISFIED/INSUFFICIENT_EVIDENCE. For
`REPEATED_SAME_SURFACE_REGRESSION` specifically there is no companion
target to "satisfy" -- the pattern's entire signal is "this
repeatedly-risky surface is being touched again," which is itself the
condition being reported, not a pass/fail check against some second
surface. **Decision: for this pattern kind, a real application is
always constructed with `status = UNSATISFIED`** (documented
precisely as: "no companion exists to satisfy for this pattern kind;
UNSATISFIED here means 'the risk condition is present,' not 'a
companion is missing'"). `SATISFIED` and `INSUFFICIENT_EVIDENCE` are
kept on the enum for forward documentation (companion/contract/test
learning kinds, once safely reconstructable, would have a real
companion-presence check to report SATISFIED/INSUFFICIENT_EVIDENCE
against) but are never constructed in v1 -- same precedent as N's own
never-constructed `SAME_FILE`. Per spec section 22 ("do not publish
SATISFIED as praise/noise"), this is also the *correct* choice even if
a satisfaction check existed: only non-satisfied applications are ever
surfaced.

## 10. Dedup ownership with N (and J/K/L/M transitively)

Whenever O's own precondition holds for an anchor (>= 2 independent
trusted findings on that exact surface), N's own query -- reused
directly -- necessarily also has at least one trusted record for that
same surface, and N's own matching (`SAME_SYMBOL`, since the anchor is
by definition directly changed whenever an application fires) will
independently construct its own `PotentialHistoricalRegression`
candidate(s) for it (bounded by N's own
`MAX_HISTORICAL_RECORDS_PER_SURFACE`). So for this pattern kind, a real
`PotentialRepositoryLearningApplication` will, in practice, always have
a same-surface N candidate to enrich. The implementation does not
hard-code this assumption, however: it takes the historical regression
report's own candidates as an explicit parameter and looks for a
same-surface match the same defensive way N looks for a same-surface
J/K/L match, falling back to `stands_alone = True` if none is found
(e.g. if a caller omits the historical report parameter, or a future
change to N's own bounds changes this). This matches the spec's own
"O should not create another competing historical warning; prefer O
as enrichment context where current underlying issue already has an
owner" instruction directly -- O never publishes a second, separate
warning block for a surface N already flags; it *always* enriches
here in v1.

## 11. Renames/moves

Not attempted -- exactly N's own documented limitation (a rename
breaks `(file_path, qualified_name)` identity; the old anchor simply
stops matching against the current pool and the learning quietly
becomes irrelevant to the current PR, never a false match). No special
handling code, no fallback.

## 12. Repository isolation / fork identity

Inherited directly from N's own query (`repository_id` filter on both
the raw `feedback_events` and the joined `review_runs`) -- O adds no
new cross-repository surface at all since it performs no query of its
own beyond calling N's function.

## 13. Security findings

No special-casing: `finding_category` is carried through unchanged
from the underlying `HistoricalRegressionRecord` (itself from
`AIFindingModel.category`), exactly like N. A repeated security
finding on one surface is exactly the case this milestone is *most*
valuable for, and it needs no different code path.

## 14. Versioning plan

- `REPOSITORY_LEARNINGS_VERSION = 1` (new).
- `REVIEW_PROMPT_VERSION`: 8 -> 9 (new `<repository_learning>` section).
- `TELEMETRY_SCHEMA_VERSION`: 6 -> 7 (new telemetry field).
- `HISTORICAL_REGRESSION_MEMORY_VERSION`/`TEST_INTELLIGENCE_VERSION`/
  `INTENT_VERIFICATION_VERSION`/`CONTRACT_INTELLIGENCE_VERSION`/
  `CHANGE_INTELLIGENCE_VERSION`/`REVIEW_POLICY_VERSION`/
  `REVIEW_ENGINE_VERSION` all unchanged -- none of their own logic
  changes.

## 15. Corpus approach

Real DB-backed staging, mirroring N's own `_stage_historical_finding`/
`_stage_feedback` helpers exactly (real `ReviewRunModel`/
`AIFindingModel`/`ReviewCandidateModel`/`FeedbackEventModel` rows) --
never FakeLLM output standing in for ground truth. Minimum 25
scenarios per spec section 40, including the mandatory "N alone (1
event) never activates an O learning; 2 independent events do" proof
and a true temporal-replay proof mirroring N's own T1/T2/T3/T4
pattern.

## 16. Integration point

Computed in `patchfrog/review/service.py::_execute_and_persist`,
immediately after the Historical Regression Memory report (last in
the J->K->L->M->N->O sequence), consuming N's own already-fetched
trusted records where possible to avoid a second identical SQL query
in the same run -- see implementation section on exact reuse.

## 17. External-review correction round (before merge)

PR #47's first shape had two real semantic gaps, both found in
external review before merge, neither caught by the (passing) 60-test
first-build corpus -- because both were errors in what the tests
*asserted as correct*, not gaps the tests failed to cover.

### 17a. `UNSATISFIED` wrongly modeled repeated history as an invariant

The original `PotentialRepositoryLearningApplication` carried
`status: RepositoryLearningApplicationStatus`, and every real
application for `REPEATED_SAME_SURFACE_REGRESSION` was constructed
with `status = UNSATISFIED`. This is semantically wrong: "this exact
surface has produced trusted findings across multiple independent
reviews" is historical-pattern *evidence*, not a requirement the
current PR can satisfy or violate. `UNSATISFIED` reads as "the current
PR fails something" -- it does not; the anchor being touched again *is*
the entire signal, and O must not describe mere recurrence as a
violated repository invariant.

**Fix**: removed `status` from `PotentialRepositoryLearningApplication`
entirely. `RepositoryLearningApplicationStatus` (`SATISFIED`/
`UNSATISFIED`/`INSUFFICIENT_EVIDENCE`) is kept on the domain module for
forward documentation only -- reserved for a genuinely relational
future pattern kind (anchor -> required companion) with a real
presence check to evaluate against -- and is never referenced by the
application dataclass in v1. All Change Story/prompt-evidence wording
was re-audited to remove any "unsatisfied"/"violates"/"missing"
framing; corpus assertions now check `not hasattr(application,
"status")` directly.

### 17b. O could stand alone -- became a second historical-regression detector

The original `derive_repository_learning_applications` took its own
`change_units` parameter and independently re-derived current-PR
relevance (its own `ChangeKind.TEST` walk over `changed_candidates`),
constructing a standalone application whenever a learning's anchor was
directly touched -- with `enriches_historical_regression` merely
*optional*. This made O a second, independent historical-regression
detector: it could fire even when Milestone N itself found nothing
relevant for the current PR, directly violating the spec's own "O must
not simply wrap N under another label... O must never independently
rediscover historical relevance."

**Fix**: `derive_repository_learning_applications` no longer takes
`change_units` at all. It requires an existing Milestone N
`PotentialHistoricalRegression` candidate on the *exact* same surface
this run -- `enriches_historical_regression` is now mandatory, not
optional, and a learning with no matching N candidate produces no
application at all (the `stands_alone` property was removed as dead
code). This also means every current-relevance rule N itself already
established (direct-change vs. affected-surface, the `ChangeKind.TEST`
exclusion, N's own dedup ownership against J/K/L) is inherited for
free -- O never re-implements any of it.

A related, narrower gap was fixed alongside 17b: `RepositoryLearning`
pattern identity took `finding_category` from an arbitrary (earliest)
supporting record while grouping purely on `(file_path,
qualified_name)`. Two trusted findings on the same symbol but a
genuinely different category (e.g. a SECURITY and an unrelated
CORRECTNESS finding) could silently combine into one fabricated
"repeated pattern." **Fix**: `finding_category` is now part of the
grouping key and the deterministic `learning_id` hash -- two findings
only support the same learning when they share it. New corpus
scenarios prove: SECURITY + CORRECTNESS on one surface never combine
(`test_case_mixed_category_same_surface_no_combined_learning`); two
SECURITY events activate a SECURITY learning; two CORRECTNESS events
activate a CORRECTNESS learning; a real, active learning with no
current N candidate produces no standalone application
(`test_case_active_learning_without_n_candidate_produces_no_standalone_application`).

### 17c. Publication-level dedup: no separate summary block in v1

Because every real application now enriches an existing N candidate on
the exact same surface, the original standalone `### Repository
learning` publication block (`summary.py`, plus
`repository_learning_summary_rendered`/`_text` columns and the
matching `publishing/body.py`/`planner.py`/`service.py` parameter)
would render immediately next to N's own `### Historical context`
block about that very surface -- saying, in effect, the same thing
twice. **Fix**: removed `summary.py` and the standalone block entirely;
this package's v1 user-facing footprint is limited to the bounded
Change Story addendum (reworded: "Repository history: ... has produced
trusted findings across N independent reviews," never "unsatisfied"),
bounded per-candidate `<repository_learning>` prompt evidence, and
count-only telemetry/persistence (two columns, not four -- no rendered
-text column at all). Migration `0023_repository_learnings` was edited
in place (never merged, so no new migration needed) to add only the
two count columns.

### 17d. Corrected corpus and gates

30 behavioral corpus scenarios (25 original + 5 new: mixed-category
negative, two category-specific positive controls, no-standalone
-without-N proof, no-invariant-status proof replacing the removed
always-unsatisfied test) + 34 unit tests, all passing. Full suite
1739/1739 (see final report). `REPOSITORY_LEARNINGS_VERSION` stays `1`
(never bumped mid-correction, since unmerged); `REVIEW_PROMPT_VERSION`
9 and `TELEMETRY_SCHEMA_VERSION` 7 both stand -- the `<repository_learning>`
prompt section and the `repository_learnings` telemetry field are both
still real, genuine additions, just with a corrected internal shape.
