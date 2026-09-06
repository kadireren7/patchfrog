# Cross-PR Intelligence Foundation

`patchfrog/cross_pr_intelligence/` answers a *spatial* question,
complementing Trajectory Intelligence's *temporal* one: **"is another
PR in this same repository concurrently touching the exact same
surface?"** -- never "whose change is wrong."

**Cross-PR overlap is not a finding.** "Also changed in PR #42" is
never published as a standalone warning, never a developer-ranking or
blame signal, never "PR #42 got there first." Cross-PR Intelligence may
only ever *deepen existing review mechanisms* for a candidate that
already exists: require critic verification, contribute toward a
higher effort tier, or reorder which candidate gets dispatched first.
It never says "this conflicts with PR #42" -- it says, at most, "treat
this only as a reason to check for conflicting or duplicated behavior."

## Same-repository only

A hard filter (`PullRequestModel.repository_id == repository_id`) --
never a cross-repository comparison. A peer PR in a different
repository is excluded even if it happens to touch a symbol with an
identical name.

## No GitHub API crawler, no polling loop

Peer eligibility and structural surfaces are read entirely from
already-ingested webhook state (`pull_requests`) and already-persisted
review history (`review_generations`/`review_candidates`) -- the exact
same tables Trajectory Intelligence already reuses. **Zero new GitHub
API calls, zero polling.**

## A necessary precursor fix: `PullRequestModel.state` was not trustworthy

Before this milestone, `PullRequestModel.state` was only ever updated
by the `opened`/`reopened`/`synchronize` webhook path -- GitHub's real
`pull_request.closed` webhook (fired on both merge and non-merge close)
had no corresponding `PullRequestEventAction` member at all and was
silently discarded. A PR's `state` column stayed frozen at `"open"`
indefinitely after it was actually closed or merged.

This was fixed as a necessary precondition for this milestone's own
"closed/merged peers must be excluded" requirement: `PullRequestEventAction.CLOSED`
was added, and `PullRequestIngestionService.ingest_closed()` persists
`state = "merged" if event.merged else "closed"` directly from the
already-verified webhook payload -- making it the *cheapest* ingestion
path (zero GitHub API calls, since there is no diff to fetch and
nothing to review). See `validation/cross_pr_intelligence/latest-summary.md`
section 2 for the full detail.

## Peer eligibility

A PR in the same repository is a peer only if **all** of:

1. `state == "open"` (now trustworthy, per the fix above).
2. It has a real reviewed generation (`ReviewGenerationModel`) --
   never reviewed means no structural surface exists to compare.
3. That generation's `commit_sha` exactly matches the peer's own
   currently-known `head_sha` -- otherwise the peer has moved past
   what was last reviewed, and its structural surfaces are stale.
   **Fail closed**: excluded, never assumed current. Verifying this
   gap via real git ancestry (like Trajectory Intelligence does for
   the *current* PR's own lineage) would require a new git operation
   against a repository/ref this run has no other reason to touch --
   forbidden by the same zero-new-git-operations discipline every
   Intelligence package upholds. The head-sha comparison achieves the
   same safety property from already-persisted data alone.

Peers are ordered by `updated_at` descending (most recently active
first), tie-broken by `github_pr_number` descending, before
`MAX_CROSS_PR_PEERS` bounds the list.

**All of this is expressed in one bounded SQL query** (`fetch_cross_pr_peers`):
a subquery aggregates `MAX(sequence_number)` grouped by
`pull_request_id` (reusing `ReviewGenerationModel`'s existing unique
index on exactly those two columns) and joins back to the exact
`(pull_request_id, sequence_number)` row to get the *authoritative*
latest generation -- structurally incapable of falling back to an
older generation whose `commit_sha` happens to match by coincidence.
Every eligibility condition lives in that one query's `WHERE` clause,
with `ORDER BY`/`LIMIT` applied before any row reaches Python -- never
an unbounded scan of every open PR in the repository followed by a
per-candidate Python loop. An external review of the original v1 shape
found exactly that unbounded-scan pattern; empirically verified fixed
(exactly one SQL statement, regardless of how many open-but-ineligible
peers exist) -- see `validation/cross_pr_intelligence/latest-summary.md`
section 19.

**Deliberately never consults `ancestry_verified`.** Unlike Trajectory
Intelligence's own lineage walk (which cares whether a *chain* of
generations is provably connected), Cross-PR Intelligence cares only
about a peer's *exact current* reviewed head. A peer whose latest
generation has `ancestry_verified=False` (that peer's own review
history included a force-push) is still a fully valid peer as long as
its `commit_sha` matches its currently-persisted `head_sha` exactly.

A composite index, `ix_pull_requests_repo_state_updated` on
`(repository_id, state, updated_at, github_pr_number)` (migration
`0026_cross_pr_peer_index`), serves this query's exact `WHERE`/
`ORDER BY`/`LIMIT` shape.

## Exact surface identity

`(file_path, qualified_name)`, never `symbol_id`. Same-file
-different-symbol never overlaps -- the key includes the qualified
name, not just the path.

## Supported and deferred overlap kinds

**Only `SAME_CHANGED_SYMBOL` is implemented in v1**: both the current
PR and a peer's latest reviewed head directly change the exact same
symbol.

Deferred (kept on `CrossPROverlapKind` for forward documentation only):

- **`SHARED_AFFECTED_SURFACE`** / **`SHARED_CONTRACT_SURFACE`** /
  **`CONTRACT_PRODUCER_CONSUMER_COLLISION`** -- Change Intelligence's
  `AffectedSymbolRef` and Contract & Blast Radius Intelligence's
  `ContractDelta` are both computed in-memory only, once per run, and
  never persisted. There is no historical row to compare a peer's past
  review against, so these three overlap kinds cannot be safely
  reconstructed for a peer -- the same "one safely provable pattern,
  defer the rest" precedent Repository Learnings and Trajectory
  Intelligence both already established.

Unlike Trajectory Intelligence's `REPEATED_SURFACE_CHURN` (which
requires >= 3 distinct heads before a signal exists, since repeated
single-PR edits are common enough at low counts to be noise), **a
single real `SAME_CHANGED_SYMBOL` overlap is sufficient** to produce a
`CrossPRSignal` -- two different PRs directly touching the exact same
symbol concurrently is precisely the concurrent-change evidence this
package exists to surface, not noise at N=1.

## No standalone publication, no user-facing copy beyond prompt evidence

No Change Story addendum, no Change Map involvement, no summary block.
The only user-facing footprint is a bounded `<cross_pr_intelligence>`
prompt section attached to the exact candidate whose surface has a
real overlap -- neutral wording only ("treat this only as a reason to
check for conflicting or duplicated behavior"), never a conclusion,
never a developer name.

## Orchestration integration (reuses Trajectory Intelligence's exact mechanism)

A new optional `cross_pr_signal_present: bool` parameter on
`ReviewEffortPolicy.decide_provisional`:

- Contributes `ReviewEffortReason.CROSS_PR_OVERLAP_PRESENT` to the
  existing signal count -- can independently push LIGHT -> STANDARD,
  or combine with another signal (including Trajectory's own) toward
  DEEP -- exactly like any other structural signal.
- Unconditionally raises `critic_expectation` to
  `CriticExpectation.MANDATORY` for that exact candidate.

`CrossPRReviewHint.REQUIRE_CRITIC` is the only hint v1's single overlap
kind ever selects. A candidate whose surface selects `REQUIRE_CRITIC`
from *either* Trajectory Intelligence or Cross-PR Intelligence is
reordered to the front of the deterministic candidate dispatch list.

**No candidate, no call** (structurally enforced): the cross-PR hint is
only ever consumed inside `_review_candidate`, called once per
candidate that already survived incremental-skip/budget filtering.

`QUALITY_COST_POLICY_VERSION` bumped 2 -> 3 -- a second real
tiering-policy semantics change, the same class of change Trajectory
Intelligence's own 1 -> 2 bump was.

## No ancestry-verification threading (unlike Trajectory Intelligence)

Trajectory Intelligence needs `previous_generation_ancestry_verified`
threaded through the whole call chain because it reasons about *this
run's own* lineage continuity. Cross-PR Intelligence reasons about
*other* PRs' independently-persisted state -- peer eligibility is
entirely self-contained inside `patchfrog/cross_pr_intelligence/queries.py`,
decided fresh from already-persisted data every call. No new parameter
was added to `review_local`/`review_pull_request`/`_run`'s public
signatures.

## Relationship to P/N/O/J/K/L/M

Every prior Intelligence package reasons about the current PR's own
history (P) or the repository's cross-review trust history (N/O), or
extends the current PR's own structural graph (J/K/L/M). Cross-PR
Intelligence is the first to reason about *other, concurrently open*
PRs. It never re-derives or reinterprets any prior package's own rules
-- it reads Change Intelligence's already-computed `ChangeUnit.changed_candidates`
directly for the current PR's own surfaces, and reads the same
`ReviewCandidateModel` rows Trajectory Intelligence already reads, just
scoped to a different PR's own review run.

## Persistence

No new table. Six bounded, nullable-default *count* columns on
`review_runs` (migration `0025_cross_pr_intelligence`):
`cross_pr_peer_count`, `cross_pr_overlap_count`, `cross_pr_signal_count`,
`cross_pr_same_changed_symbol_count`, `cross_pr_require_critic_count`,
`cross_pr_deepen_context_count`. No PR number/author/title column
anywhere -- privacy discipline matches every other Intelligence
package's own telemetry.

## Limitations

- Only `SAME_CHANGED_SYMBOL` is implemented; the other three overlap
  kinds are deferred (see above).
- Stale-peer-head detection is a head-sha comparison, not a real git
  ancestry proof -- deliberately conservative (fail closed on any
  mismatch) rather than a new cross-repository git operation.
- Cross-*repository* intelligence of any kind is explicitly out of
  scope for v1.
- A cross-PR overlap is orchestration evidence, not proof -- it can
  only ever deepen scrutiny of an already-real candidate; it never
  manufactures one, and it never tells the reviewer which PR is
  "right."
