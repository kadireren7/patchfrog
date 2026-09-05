# Milestone P: Trajectory Intelligence Foundation -- Pre-Implementation Audit

Written before any code. Answers the spec's own audit questions before
implementation begins, exactly like J/K/L/M/N/O's own audits.

## 1. What PR evolution history does PatchFrog already persist?

The critical discovery this audit turns on: `patchfrog/persistence/models/review_memory.py::ReviewGenerationModel`
already IS a durable, ordered, ancestry-verified history of every
review generation for a PR -- built by Phase 7 (`patchfrog.review_memory`)
for a completely different purpose (incremental candidate skipping /
finding carry-forward), but structurally exactly the data Trajectory
Intelligence needs. One row per successfully-finalized review run for
a PR:

- `sequence_number` -- 1-based, strictly increasing per
  `pull_request_id`. The *only* safe ordering key (never `created_at`,
  which can tie within the same timestamp tick -- see the column's own
  docstring).
- `commit_sha` -- this generation's exact head.
- `previous_generation_id` -- FK to the generation this one was
  computed against (nullable: first generation for a PR).
- `previous_commit_sha` -- denormalized copy of that generation's
  `commit_sha`.
- `ancestry_verified: bool` -- whether `previous_commit_sha` was
  proven (via real git plumbing, `patchfrog.repository.ancestry.verify_ancestor_with_diff`,
  run in `patchfrog.review_memory.service.IncrementalReviewMemoryService.prepare`)
  to be a genuine git ancestor of `commit_sha`. **This is Phase 7's own
  force-push detection, already computed, already persisted.**
- `review_run_id` -- FK to `ReviewRunModel` (unique per generation),
  which itself has `repository_index_id` (that generation's own
  indexed snapshot) and `started_at`.

`PullRequestModel` itself only stores the *current* `base_sha`/`head_sha`
(mutated in place on every webhook update) -- it is not a history
table. `ReviewGenerationModel` is the only durable multi-head record.

## 2. How many previous exact heads can be reconstructed without GitHub crawling?

As many as `ReviewGenerationModel` rows exist for the PR with an
unbroken `ancestry_verified` chain, bounded by `MAX_TRAJECTORY_HEADS`
(8). Zero new GitHub API calls, zero new git operations: Phase 7
already ran `verify_ancestor_with_diff` once per generation, at the
time each one was created, and persisted the true/false result. **The
transitivity argument**: if generation G2's `ancestry_verified=True`
proves `G1.commit_sha` is an ancestor of `G2.commit_sha`, and G1's own
`ancestry_verified=True` proves `G0.commit_sha` is an ancestor of
`G1.commit_sha`, then `G0.commit_sha` is (by pure graph transitivity,
not re-verified) also an ancestor of `G2.commit_sha` -- git parent
pointers are immutable once committed, so chaining already-independently
-verified one-hop links needs no re-verification. Walking
`previous_generation_id` backward from the PR's latest existing
generation, stopping the moment a step's `ancestry_verified` is
`False` (or the chain ends), gives a fully trustworthy lineage with
**zero new git plumbing calls**.

## 3. Can symbol/file trajectory be derived from existing review memory?

Yes, for the "which surface changed" question, via
`ReviewCandidateModel` rows already persisted per `review_run_id`
(`file_path`, `qualified_name`, `reason`). A candidate with
`reason == ReviewCandidateReason.CHANGED_SYMBOL` at a given historical
generation is exactly a "this surface was actually edited at this
head" fact -- no new indexing, no diff recomputation, one bounded
query across the valid generations' `review_run_id`s.

`qualified_name` (a plain string) is the safe identity key across
generations -- never `symbol_id` (a fresh UUID per repository index,
unstable across re-indexing, exactly the same trap N's and O's own
audits already ruled out).

## 4. Can force-push safely invalidate trajectory?

Yes, for free -- see section 2. A generation whose own
`ancestry_verified` is `False` means *this* generation's link to its
predecessor is broken (force-push, or the predecessor was otherwise
unprovable); the walk stops there. Nothing before that point is ever
connected to the current lineage.

## 5. What exact commit/review ordering is trustworthy?

`sequence_number`, never `created_at`, never UUID, never lexicographic
SHA -- exactly `ReviewGenerationModel`'s own established invariant,
reused verbatim.

## 6. Can reverted surface behavior be detected structurally? (REVERT_LIKE_CYCLE)

**Deferred.** A safe A -> B -> A structural fingerprint would need a
stable per-surface content fingerprint at each historical head.
`SymbolModel.content_hash` exists and *is* retained indefinitely (old
`RepositoryIndexModel` rows are never deleted -- only `is_active`
moves, see `patchfrog.persistence.repositories.repository_index`), so
the raw data technically exists. But Phase 7's own `symbol_continuity.match_symbols`
(the only place in this codebase that safely compares symbol identity
across two indexes) is explicitly restricted to *adjacent* generations
in an already-verified incremental chain -- comparing content hashes
across *non-adjacent* historical heads would require re-deriving a new
matching mechanism this codebase does not have and has deliberately
never built for arbitrary-distance comparison (the same "no fuzzy
symbol matching across indexes" boundary N's and O's own audits
already drew). Faking it with a raw `qualified_name` + `content_hash`
join (skipping proper symbol continuity) risks exactly the false
-positive class this milestone's own principle forbids. Deferred, not
implemented -- kept on `TrajectorySignalKind` for forward documentation
only.

## 7. Can same-surface repeated edits be detected? (REPEATED_SURFACE_CHURN)

**Yes -- the only pattern implemented in v1.** Exactly section 3's
mechanism: for each valid historical generation in the current PR's
verified lineage, plus the current (in-progress) head's own
already-computed `ChangeUnit.changed_candidates` (Change Intelligence
has already run earlier in `_execute_and_persist` -- no second
computation), collect `(file_path, qualified_name)` for every
`CHANGED_SYMBOL` candidate. Group by exact surface; a surface touched
across `>= MIN_SURFACE_CHURN_EVENTS` (3) *distinct* heads (deduplicated
by `commit_sha` -- see section 9) is `REPEATED_SURFACE_CHURN`.

## 8. Can test-after-production trajectory be detected? (PRODUCTION_THEN_TEST_FOLLOWUP)

**Deferred, on reflection -- not implemented in v1.** `is_test_path`
does let every historical/current candidate be classified test vs.
production for free (no new data), which is enough to prove a test
file was touched at a later head than a production file. But the spec
itself requires more than temporal ordering: "production behavior
surface *repeatedly* changes then *related* test surface changes" --
proving the test is actually *related* to the specific production
surface (not just "some test file, somewhere, changed later") is
exactly J's `ExpectedCompanionChange`/M's `TestExpectation` own
relation logic, which -- like K's `ContractDelta` (section 10) -- is
computed in-memory only per review run and never persisted as a
structured row for a historical head. Reconstructing it historically
would mean either re-deriving that relation logic against old diffs
(a new, unbounded per-head computation this milestone's own "no new
history crawler" principle rules out) or silently downgrading the
claim to bare temporal coincidence, which would misrepresent an
unrelated pair of edits as a meaningful trajectory signal -- exactly
the kind of fabricated confidence "do not fake this feature" forbids.
Deferred; kept on `TrajectorySignalKind` for forward documentation
only. **v1 therefore implements exactly one signal kind:
`REPEATED_SURFACE_CHURN`** -- the same "one safely-provable pattern,
the rest deferred" discipline Milestone O's own audit already
established.

## 9. Can removed-then-reintroduced surfaces be detected? (REINTRODUCED_SURFACE)

**Deferred**, for the same reason as REVERT_LIKE_CYCLE (section 6):
proving a symbol was genuinely *removed* (not merely renamed/moved)
and then a *different* commit reintroduces the *same* logical symbol
requires cross-generation symbol continuity this codebase's existing
`symbol_continuity` module deliberately restricts to adjacent pairs.
Kept on `TrajectoryEventKind`/`TrajectorySignalKind` for forward
documentation only.

## 10. What cannot be proven and must be deferred?

- `REVERT_LIKE_CYCLE` (section 6).
- `REINTRODUCED_SURFACE` (section 9).
- `CONTRACT_SURFACE_CHANGED` as its own event kind -- Contract & Blast
  Radius Intelligence's own `ContractDelta` objects are computed
  in-memory only per review run and never persisted as structured
  rows (the exact same limitation O's own audit found for
  companion/consumer identity); only a bounded aggregate count
  (`review_runs.contract_delta_count`) survives, which cannot identify
  *which* historical candidate was itself a contract change. Deferred;
  kept on `TrajectoryEventKind` for forward documentation only.
- Cross-PR trajectory of any kind (explicitly out of scope per spec
  section 3).
- Rename/move continuity across non-adjacent heads (inherits the same
  limitation N/O already documented).

## 11. Can P avoid adding a GitHub commit-history crawler?

Yes, entirely -- see sections 1-5. Zero new git operations, zero new
GitHub API calls. `ReviewGenerationModel`/`ReviewCandidateModel`
(already-persisted Phase 5/Phase 7 data) are the *only* two tables
this package reads.

## 12. Where can trajectory safely influence orchestration today?

`patchfrog.review.effort.ReviewEffortPolicy.decide_provisional` already
combines multiple independent structural signals (static findings,
security relevance, symbol size, changed-line count) into exactly one
`ReviewEffortTier` + `CriticExpectation`, via the existing
`MULTIPLE_STRUCTURAL_SIGNALS` corroboration rule -- this is the one
correct integration point, not a new parallel decision path. A new
optional `trajectory_hint: TrajectoryReviewHint` parameter is added:

- `TrajectoryReviewHint.REQUIRE_CRITIC` (the only hint any v1 signal
  ever selects -- mapped from `REPEATED_SURFACE_CHURN`, the only
  implemented signal kind) contributes a new
  `ReviewEffortReason.TRAJECTORY_SIGNAL_PRESENT` to the existing
  signal count (so it can independently push LIGHT -> STANDARD or
  contribute toward `MULTIPLE_STRUCTURAL_SIGNALS` -> DEEP, exactly
  like any other structural signal -- never a separate escalation
  path) **and** unconditionally raises `critic_expectation` to
  `MANDATORY` for that exact candidate, the same way DEEP already
  does today -- reusing the existing `CriticExpectation` enum and
  `patchfrog.review.critic_selection` machinery completely unchanged.
  Any candidate whose surface selects this hint is *also* reordered to
  the front of the deterministic candidate list in
  `patchfrog.review.service._execute_and_persist`, so its
  `asyncio.Semaphore` acquisition and shared token-budget claim happen
  earlier -- a zero-cost, safe ordering effect, not a separate hint
  value.
- `TrajectoryReviewHint.DEEPEN_CONTEXT`/`INCREASE_CANDIDATE_PRIORITY`
  are kept on the enum for forward documentation only -- reserved for
  `PRODUCTION_THEN_TEST_FOLLOWUP`/`REINTRODUCED_SURFACE`/
  `REVERT_LIKE_CYCLE` once (if ever) safely implementable -- but are
  never selected by v1's single implemented signal kind, exactly like
  N's own never-constructed `HistoricalMatchKind.SAME_FILE`.
- `TrajectoryReviewHint.NONE` -- no effect at all; a quiet PR's
  behavior is byte-identical to before this milestone.

**No candidate, no call** (mandatory): trajectory hints are only ever
consumed inside `_review_candidate`, called once per candidate that
already survived incremental-skip/budget filtering. A candidate that
never reaches this method (skipped, budget-exhausted) never triggers
any trajectory-driven effect -- structurally impossible to bypass,
since the hint lookup and `decide_provisional` call both live inside
the same method that is the *only* place a provider call can originate.

`QUALITY_COST_POLICY_VERSION` (the dedicated version for exactly this
kind of tiering-semantics change, per its own docstring: "can
invalidate canonical-run reuse without needing a broader engine- or
policy-version bump") is bumped 1 -> 2. `REVIEW_ENGINE_VERSION` (call
shape/retry/execution architecture) is unchanged -- no new call shape,
no new retry rule, no new agent role.
