# Trajectory Intelligence Foundation

`patchfrog/trajectory_intelligence/` answers a different kind of
question than every prior Intelligence package in this lineage:
**"where should PatchFrog spend more review attention?"** -- never
"where is the bug?"

**Trajectory signals are not findings.** "Changed 4 times," "same
symbol edited across several commits," "test added after repeated
production edits" -- none of these are ever published as a standalone
warning. Trajectory Intelligence may only ever *deepen existing review
mechanisms* for a candidate that already exists: require critic
verification, contribute toward a higher effort tier, or reorder which
candidate gets dispatched first. It never says "this function changed
5 times, therefore it is buggy" -- it says, at most, "treat this only
as a reason to inspect current evidence carefully."

## Current-PR-only scope

v1 has zero cross-PR trajectory. No previous unrelated PR history, no
contributor history, no repository-lifetime churn, no developer
-behavior analytics. Scope is exactly: the current PR's own lineage,
from whatever previously-reviewed heads are still verifiably connected
to it, up to the current exact head.

## Zero new git operations, zero new history crawler

This package reuses Phase 7's own already-persisted, already
-ancestry-verified `review_generations` lineage directly. See
`validation/trajectory_intelligence/latest-summary.md` sections 1-5,
11 for the full audit. **No new table, no new git plumbing call, no
new GitHub API call.** The one bounded query this package issues reads
`review_generations` (Phase 7) and `review_candidates` (Phase 5) --
both already-existing tables.

## Lineage / force-push safety

`ReviewGenerationModel.ancestry_verified` is Phase 7's own real
git-plumbing force-push detection, computed once per generation, at
creation time. Walking `previous_generation_id` backward from the PR's
latest existing generation, stopping the moment a step's
`ancestry_verified` is `False` or the chain ends, gives a fully
trustworthy lineage with zero re-verification -- already
-independently-verified one-hop links chain together by pure graph
transitivity (git parent pointers are immutable once committed).
Ordering is always `sequence_number`, never `created_at`/UUID/SHA
text.

A force-push detected when a generation was created means that
generation's own link to its predecessor is unusable -- everything
before that point is simply excluded from the current lineage, never
guessed at or connected across the break.

**The persisted chain above is only half the picture.** This package
always runs *before* the current, in-progress review's own
`ReviewGenerationModel` row exists (Phase 7's `finalize()` only creates
it after the AI review completes and persists). The current head is
represented as a synthetic entry, appended to the persisted chain --
and the edge from *the latest persisted generation* to *this exact,
in-progress commit* is a **separate edge that must itself be proven**,
never assumed just because every generation before it verified
cleanly. An external-review correction round found the original v1
shape skipped this proof entirely, creating a real force-push hole
(see `validation/trajectory_intelligence/latest-summary.md` section
17). Fixed by reusing Phase 7's own already-computed answer for this
exact edge -- `PreparedReview.plan.selection.ancestry_verified` from
this run's own `IncrementalReviewMemoryService.prepare()` call, threaded
through as `previous_generation_ancestry_verified` -- never a second,
re-derived ancestry check. `build_trajectory_intelligence_report`
combines historical and current heads only when that flag is `True`
(or when the current commit is a same-SHA retry/replay of the latest
persisted head, which needs no proof at all); otherwise it fails
closed, discarding any persisted lineage and analyzing the current
head alone. `TrajectoryIntelligenceReport.lineage_valid` reflects
exactly this: whether the lineage *actually used* for this review
includes a proven historical connection, never merely whether
historical generations exist for the PR.

## Exact surface identity

`(file_path, qualified_name)`, never `symbol_id` (a fresh UUID per
repository index, unstable across re-indexing -- the same trap every
prior Intelligence package's own audit already ruled out). No
`SAME_FILE`-style fallback, no fuzzy symbol matching.

## Supported and deferred signal kinds

**Only `REPEATED_SURFACE_CHURN` is implemented in v1**: the exact same
surface touched across at least `MIN_SURFACE_CHURN_EVENTS` (3) distinct
heads (deduplicated by `commit_sha`) in the current PR's own verified
lineage.

Deferred (kept on `TrajectorySignalKind`/`TrajectoryEventKind` for
forward documentation only):

- **`REVERT_LIKE_CYCLE`** -- an A -> B -> A structural fingerprint
  would need cross-generation symbol-continuity comparison this
  codebase deliberately restricts to *adjacent* generations
  (`patchfrog.review_memory.symbol_continuity`). Comparing across
  arbitrary-distance historical heads would mean re-deriving a new
  matching mechanism this codebase does not have.
- **`REINTRODUCED_SURFACE`** -- same limitation: proving a symbol was
  genuinely removed (not renamed/moved) then reintroduced needs the
  same cross-generation continuity work.
- **`PRODUCTION_THEN_TEST_FOLLOWUP`** -- proving a test is actually
  *related* to a specific production surface (not just "some test file
  changed later") is exactly Change/Test Intelligence's own relation
  logic, which is computed in-memory only per review run and never
  persisted for a historical head.
- **`CONTRACT_SURFACE_CHANGED`** (event kind) -- Contract & Blast
  Radius Intelligence's own `ContractDelta` objects have the same
  never-persisted-per-historical-head limitation.

Test-vs-production classification (`SURFACE_CHANGED`/
`TEST_SURFACE_CHANGED`) uses `patchfrog.indexing.inventory.is_test_path`
-- a pure function of `file_path`, no new data needed -- but a test
file churning repeatedly is treated identically to a production file
churning repeatedly in v1: both can trigger `REPEATED_SURFACE_CHURN`.

## No standalone publication, no user-facing copy in v1

Unlike every prior Intelligence package, this one has **no** Change
Story addendum, no Change Map involvement, no conditional summary
block at all. Trajectory Intelligence's only user-facing footprint is
a bounded `<trajectory_intelligence>` prompt section attached to the
exact candidate whose surface has a real signal -- neutral wording only
("treat this only as a reason to inspect current evidence carefully"),
never a conclusion.

## Orchestration integration

The one correct integration point is
`patchfrog.review.effort.ReviewEffortPolicy.decide_provisional`, which
already combines independent structural signals (static findings,
security relevance, symbol size, changed-line count) via its existing
`MULTIPLE_STRUCTURAL_SIGNALS` corroboration rule. A new optional
`trajectory_signal_present: bool` parameter:

- Contributes `ReviewEffortReason.TRAJECTORY_SIGNAL_PRESENT` to the
  existing signal count -- can independently push LIGHT -> STANDARD,
  or combine with another signal toward DEEP -- exactly like any other
  structural signal, never a separate escalation path.
- Unconditionally raises `critic_expectation` to `CriticExpectation.MANDATORY`
  for that exact candidate, reusing `patchfrog.review.critic_selection`
  completely unchanged.

`TrajectoryReviewHint.REQUIRE_CRITIC` is the only hint v1's single
signal kind ever selects; `DEEPEN_CONTEXT`/`INCREASE_CANDIDATE_PRIORITY`
are reserved on the enum for future signal kinds. A candidate whose
surface selects `REQUIRE_CRITIC` is *also* reordered to the front of
the deterministic candidate list in `_execute_and_persist` -- earlier
`asyncio.Semaphore` acquisition, earlier shared-token-budget claim, a
zero-cost ordering effect (`TrajectoryReviewHint.INCREASE_CANDIDATE_PRIORITY`'s
own spec behavior, folded into the one hint rather than a second,
independently-selected value).

**No candidate, no call** (mandatory, structurally enforced): the
trajectory hint is only ever consumed inside `_review_candidate`,
called once per candidate that already survived incremental-skip/
budget filtering. A candidate that never reaches this method never
triggers any trajectory-driven effect.

`QUALITY_COST_POLICY_VERSION` bumped 1 -> 2 -- unlike every prior
milestone in this lineage, this is a real tiering-policy semantics
change, not merely a new optional prompt section.

## Context Engine / Critic

No second Context Engine, no new depth parameter, no bypass of
existing fanout/hop bounds -- STANDARD/DEEP already enable adaptive
context; escalating tier via the existing signal-count mechanism is
the entire "deepen context" effect. Critic strictness reuses
`CriticExpectation`/`patchfrog.review.critic_selection` completely
unchanged.

## Relationship to N/O/K/L/M

N/O reason about *cross-review* historical trust (has this repository
learned something painful about this surface, possibly repeated
independently across separate reviews). P reasons about *current-PR
-lineage* evolution only. The two are structurally orthogonal domain
types with no shared fields, no shared query, no shared state -- both
can reference the same surface without interfering, and a real
finding's own ownership is untouched: K remains the owner of
contract-consumer issues, L of intent-gap issues, M of test-gap
issues. Trajectory Intelligence never creates a second, competing
version of any of their findings; it only ever adjusts how much
scrutiny an already-real candidate receives.

## Persistence

No new table. Six bounded, nullable-default *count* columns on
`review_runs` (migration `0024_trajectory_intelligence`):
`trajectory_head_count`, `trajectory_event_count`,
`trajectory_signal_count`, `trajectory_repeated_surface_churn_count`,
`trajectory_require_critic_count`, `trajectory_deepen_context_count`.
No rendered-text column at all -- this package has no standalone
publication block.

## Limitations

- Only `REPEATED_SURFACE_CHURN` is implemented; the other three signal
  kinds are deferred (see above).
- Rename/move continuity is deferred, inherited from the same
  adjacent-generation-only restriction N/O already documented.
- Cross-PR trajectory of any kind is out of scope for v1.
- A trajectory signal is orchestration evidence, not proof -- it can
  only ever deepen scrutiny of an already-real candidate; it never
  manufactures one.
