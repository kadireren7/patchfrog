# Milestone Q: Cross-PR Intelligence Foundation -- Pre-Implementation Audit

Branch: `feat/cross-pr-intelligence`, off `main` @ `2cd9e1a31def516b969c215439357093d3ea181a`
(Milestone P, Trajectory Intelligence Foundation, merged).

## 0. Product principle (restated)

Detect meaningful *structural* overlap between the current PR and other
concurrently-relevant PRs **in the same repository** -- e.g. both PRs
directly changing the exact same `(file_path, qualified_name)` symbol.
Cross-PR overlap is evidence, never a standalone finding: it may only
ever strengthen orchestration (require critic, reorder candidates) for
a candidate that already exists, exactly the same product boundary
established by Trajectory Intelligence (Milestone P) for *temporal*
(within-PR) evidence -- this milestone is the *spatial* (across-PR)
analogue. Same-file/same-author/same-title alone is explicitly
insufficient; only a real, structurally-detected symbol-identity
overlap counts.

## 1. Audit question: what other-PR state is already persisted locally?

`PullRequestModel` (`patchfrog/persistence/models/pull_request.py`):
`repository_id`, `github_pr_number`, `title`, `author`, `base_sha`,
`head_sha`, `state`, `created_at`, `updated_at`. Unique on
`(repository_id, github_pr_number)`.

**A real, previously-undiscovered gap was found here and is now
fixed as a precursor to this milestone** (see section 2).

## 2. Discovered gap: `PullRequestModel.state` was not trustworthy, and is now fixed

Before this milestone, `state` was written **only** by
`PullRequestIngestionService.ingest()`, called only for the
`opened`/`reopened`/`synchronize` webhook actions (see
`patchfrog/services/pull_request_ingestion.py` and
`patchfrog/domain/github.py`'s `PullRequestEventAction`, which had no
`CLOSED` member at all). `patchfrog/github/webhooks.py::parse_pull_request_event`
silently returns `None` for any action not in that enum -- meaning
GitHub's real `pull_request.closed` webhook (fired on both merge and
non-merge close) was discarded entirely, and a PR's `state` column
stayed frozen at "open" indefinitely after it was actually closed or
merged.

This directly threatened this milestone's own mandatory acceptance
criterion: closed/merged peer PRs must be excluded from cross-PR
overlap consideration. Without a fix, `state == "open"` would be
worthless as an eligibility filter for *any* peer PR that was closed
or merged after its last opened/reopened/synchronize event -- which is
the common case for any PR older than the current one.

A second, narrower mechanism already existed
(`patchfrog/feedback/sync.py::GitHubFeedbackSyncService._sync_pr_lifecycle`)
that fetches fresh PR metadata and detects `state == "closed"` /
`metadata.merged` -- but it only emits an internal `FeedbackEvent` for
analytics purposes, never writes back to `PullRequestModel.state`, and
it only runs when a human explicitly invokes
`python -m patchfrog.cli feedback sync --pr <number>` for a *specific*
PR that PatchFrog has already published a review to. It does not, and
structurally cannot, keep every other PR in a repository's `state`
current -- so it does not solve this milestone's peer-eligibility
problem.

**Fix applied** (before any Cross-PR Intelligence code was written,
as a necessary correctness precondition for this milestone's own
acceptance criteria -- not a Cross-PR Intelligence behavior itself):

- `patchfrog/domain/github.py`: added `PullRequestEventAction.CLOSED = "closed"`,
  and a `merged: bool = False` field on `PullRequestWebhookEvent`
  (GitHub's own `pull_request.merged` boolean -- always present on the
  raw payload regardless of action).
- `patchfrog/github/webhooks.py::parse_pull_request_event`: parses
  `merged` unconditionally.
- `patchfrog/services/pull_request_ingestion.py`: new
  `PullRequestIngestionService.ingest_closed()` -- the cheapest
  possible path. Everything needed
  (title/author/base_sha/head_sha/merged) is already on the verified
  webhook payload itself, so unlike `ingest()` it makes **zero**
  GitHub API calls (no diff to fetch, nothing to review). Persists
  `state = "merged" if event.merged else "closed"` via the existing
  `PullRequestRepository.upsert`.
- `apps/worker/tasks/process_pull_request.py`: `_ingest` branches on
  `event.action is PullRequestEventAction.CLOSED` to call
  `ingest_closed` instead of `ingest`, and the
  `schedule_pipeline_if_eligible` call is now also gated on
  `event.action is not PullRequestEventAction.CLOSED` -- a closed/merged
  PR has no commit that should be reviewed, and must never enqueue a
  review pipeline run.
- `apps/api/routes/github_webhooks.py`: threads `merged=event.merged`
  into the Celery `.delay()` call.

This is a genuine, minimal, precedent-consistent gap-fix (GitHub
already sends this event for free; PatchFrog was just silently
discarding it) -- not a new crawler, not a new permission, not a new
poll loop. Zero new GitHub API calls are added anywhere in this fix
(the closed path is strictly cheaper than the existing open path).
Full test coverage: `tests/unit/test_webhook_parser.py` (closed/merged
parsing), `tests/unit/test_worker_task.py` (event reconstruction),
`tests/integration/test_pull_request_ingestion_service.py`
(`ingest_closed` persistence + idempotency + zero-GitHub-calls
assertion via an `_ExplodingGitHubClient`), `tests/integration/test_webhook_route.py`
(full HTTP route), `tests/integration/test_ingestion_scheduling_failure.py`
(worker-level: closed events never call `schedule_pipeline_if_eligible`).
38 tests, all passing; ruff/mypy clean on every touched file.

With this fix, `PullRequestModel.state == "open"` is now a trustworthy
signal for **any** PR in a repository PatchFrog has ingested at least
one webhook for since this fix -- not just ones it has published a
review to.

## 3. Audit question: can currently-OPEN peer PRs be determined without a new GitHub API crawler?

Yes, per section 2's fix: `select(PullRequestModel).where(repository_id == ..., state == "open", id != current_pull_request_id)`.
No new GitHub API call, no polling, no crawler -- pure read of
already-ingested webhook state.

## 4. Audit question: can the exact latest reviewed head per peer PR be identified?

Yes -- reuses the exact mechanism Trajectory Intelligence's own
`fetch_trajectory_heads` already established:
`select(ReviewGenerationModel).where(pull_request_id == peer.id).order_by(sequence_number.desc()).limit(1)`.
`ReviewGenerationModel.review_run_id` gives the peer's latest reviewed
run.

**Stale-peer-head exclusion (spec requirement, fail-closed
preferred)**: a peer's *persisted* latest reviewed head can be behind
the PR's *actual* current head (new commits pushed since the last
review). Verifying that gap via git ancestry would require a new git
operation against a repository/ref this run has no other reason to
touch -- exactly the kind of new, unbounded cross-PR git operation the
product principle forbids. The safe, zero-new-I/O alternative already
available from persisted data alone: compare
`peer_pull_request.head_sha` (kept current by every ingested webhook,
including the new `closed`/`merged` path) against the peer's latest
`ReviewGenerationModel.commit_sha`. A mismatch means the peer has moved
past what was last reviewed -- excluded (fail closed), never assumed
current.

## 5. Audit question: can changed structural surfaces per peer PR be reconstructed?

Yes -- reuses Trajectory Intelligence's own `fetch_changed_surfaces_for_heads`
query shape exactly: `ReviewCandidateModel` rows with
`reason == CHANGED_SYMBOL` and non-`None` `qualified_name`, filtered to
the peer's one latest `review_run_id`. A module-region candidate
(`qualified_name is None`) never participates -- same exclusion every
prior Intelligence package's own audit already established.

## 6. Audit question: can ContractDelta / affected-surface be reconstructed historically per peer PR?

No. Exactly the same limitation N/O/P's own audits already
established: Contract & Blast Radius Intelligence's `ContractDelta`
and Change Intelligence's `AffectedSymbolRef` are both computed
in-memory only, once per run, and never persisted as their own rows.
There is no way to reconstruct "what contract deltas did peer PR #N's
last review compute" after the fact.

**Consequence (mirrors O's and P's own "one safely provable pattern,
defer the rest" precedent exactly)**: `SHARED_AFFECTED_SURFACE`,
`SHARED_CONTRACT_SURFACE`, and `CONTRACT_PRODUCER_CONSUMER_COLLISION`
are all **deferred** in v1. `SAME_CHANGED_SYMBOL` -- the exact same
`(file_path, qualified_name)` directly changed by both the current PR
and a peer's last reviewed head -- is the only overlap kind
implemented, because it is the only one provable from already
-persisted `ReviewCandidateModel` rows with zero historical
reconstruction risk. All three deferred kinds are kept on
`CrossPROverlapKind` for forward documentation only, never
constructed, exactly mirroring `HistoricalMatchKind.SAME_FILE` (N),
`RepositoryLearningPatternKind`'s deferred members (O), and
`TrajectorySignalKind`'s deferred members (P).

## 7. Domain model

`patchfrog/cross_pr_intelligence/domain.py` (pure, no I/O -- mirrors
every prior package's own `domain.py` role):

- `CROSS_PR_INTELLIGENCE_VERSION = 1`.
- Bounds: `MAX_CROSS_PR_PEERS = 10`, `MAX_CROSS_PR_SURFACES_PER_PEER = 50`,
  `MAX_CROSS_PR_OVERLAPS = 20`, `MAX_CROSS_PR_SIGNALS = 10`. Never an
  unbounded scan of every PR in a repository's history -- peers are
  ordered by `PullRequestModel.updated_at` descending (most recently
  active peer first), tie-broken by `github_pr_number` descending
  (deterministic even when two updates land in the same
  timestamp tick -- SQLite's default `CURRENT_TIMESTAMP` resolution is
  whole seconds, which matters for test determinism even though
  Postgres's own microsecond resolution rarely ties in production)
  before the `MAX_CROSS_PR_PEERS` cap is applied.
- `CrossPROverlapKind` (`SAME_CHANGED_SYMBOL` implemented;
  `SHARED_AFFECTED_SURFACE`/`SHARED_CONTRACT_SURFACE`/
  `CONTRACT_PRODUCER_CONSUMER_COLLISION` deferred, per section 6).
- `CrossPRReviewHint` (`NONE`/`DEEPEN_CONTEXT`/`REQUIRE_CRITIC`/
  `INCREASE_CANDIDATE_PRIORITY` -- mirrors `TrajectoryReviewHint`
  exactly; only `NONE`/`REQUIRE_CRITIC` are ever selected in v1, the
  other two reserved for future overlap kinds).
- `CrossPRPeer(pull_request_id, github_pr_number, head_commit_sha, review_run_id, sequence_number)` --
  the peer PR's identity and its one latest, verified-current reviewed
  head. `github_pr_number` is carried because it is legitimate,
  useful, *non-sensitive-within-the-repository's-own-org* evidence to
  surface to a human reviewer ("this symbol is also being changed in
  PR #42") -- the same repository's own contributors already see every
  PR number in their own GitHub UI. It is **never** persisted to
  telemetry (see section 11) -- only ever used to build bounded
  per-candidate prompt evidence text, exactly like every other
  Intelligence package's own evidence text.
- `CrossPROverlap(peer, overlap_kind, file_path, qualified_name, evidence)` --
  one structural fact: this exact surface is also changed in a peer's
  reviewed head.
- `CrossPRSignal(surface_file_path, surface_qualified_name, signal_kind, supporting_overlaps, review_hint, evidence)` --
  mirrors `TrajectorySignal` exactly. Unlike Trajectory Intelligence
  (which requires >= `MIN_SURFACE_CHURN_EVENTS` distinct heads before
  a signal exists at all), **a single real `SAME_CHANGED_SYMBOL`
  overlap is sufficient to produce a signal** -- two different PRs
  directly touching the exact same symbol concurrently is not noise at
  N=1 the way repeated single-PR edits are (Trajectory's own
  `MIN_SURFACE_CHURN_EVENTS = 3` docstring rationale does not apply
  here: this is cross-author concurrent-change evidence, not
  within-PR churn).
- `CrossPRIntelligenceReport(version, peers_considered, overlaps, signals)`.

**No separate "PotentialCrossPRApplication" wrapper type.** Unlike
Repository Learnings (which only ever *enriches* an independent N
candidate, and so needed a wrapper type expressing that mandatory
relationship), a `CrossPROverlap`/`CrossPRSignal` here already *is* the
complete, self-contained structural fact -- there is no separate,
independently-detected finding it is enriching. This mirrors
Trajectory Intelligence's own decision not to introduce such a
wrapper type.

## 8. Package layout (mirrors Trajectory Intelligence exactly)

`patchfrog/cross_pr_intelligence/{__init__,domain,queries,matching,service,evidence,telemetry}.py`.
No `story.py`/`summary.py` -- per O's precedent (avoid a duplicative
user-facing block) and per P's precedent (this is orchestration
evidence, never a standalone finding/story block). Zero LLM calls
anywhere in the package (structurally enforced by a test mirroring
`test_trajectory_intelligence_never_imports_a_provider`).

## 9. Orchestration integration (reuses P's exact mechanism)

- `ReviewEffortPolicy.decide_provisional` gains `cross_pr_signal_present: bool = False`,
  counted as a structural signal exactly like `trajectory_signal_present`
  (contributes to `signal_count`, can independently push
  LIGHT -> STANDARD or combine toward DEEP), plus the same unconditional
  `critic_expectation = CriticExpectation.MANDATORY` override.
- New `ReviewEffortReason.CROSS_PR_OVERLAP_PRESENT`.
- Candidate dispatch ordering: a candidate whose surface selects
  `REQUIRE_CRITIC` from Cross-PR Intelligence is also sorted first,
  using the same stable-sort mechanism Trajectory Intelligence already
  added in `_run` -- extended to consider *either* report's hint.
- `QUALITY_COST_POLICY_VERSION` bumped **2 -> 3**: a second real
  tiering-policy semantics change (a new structural signal kind), not
  merely a new prompt section -- same justification class as P's own
  1 -> 2 bump. **No retirement work needed on J/K/L/M/N/O's own
  versioning tests**: checked each file directly -- none pins a frozen
  `== N` comparison for `QUALITY_COST_POLICY_VERSION` any more (they
  either reference the live constant directly for fingerprint
  construction, with no fixed comparison at all, or -- in
  `test_review_quality_cost_guard_versioning.py`'s own "introduced"
  test -- already assert `QUALITY_COST_POLICY_VERSION >= 1`, which
  remains true). P's own versioning test
  (`test_trajectory_intelligence_versioning.py`) asserts
  `QUALITY_COST_POLICY_VERSION > _PRE_P_QUALITY_COST_POLICY_VERSION`
  with `>`, also still true. P's correction round already made every
  one of these bump-proof; Q only adds its own new versioning test
  file with a fresh `_PRE_Q_QUALITY_COST_POLICY_VERSION = 2` frozen
  comparison point.

## 10. Prompt / evidence wording

New `<cross_pr_intelligence>` prompt section (mirrors
`<trajectory_intelligence>` exactly) -- `REVIEW_PROMPT_VERSION` bumped
10 -> 11. Evidence text is strictly neutral, never a conclusion:
"this exact surface is also being changed in PR #<n>; treat this only
as a reason to check for conflicting behavior, not as evidence of a
defect." No developer name, no ranking, no blame framing anywhere in
the text (spec constraint).

## 11. Telemetry (counts only, no PR-number/author identity)

`TELEMETRY_SCHEMA_VERSION` bumped 8 -> 9.
`CrossPRIntelligenceSummary`: `peer_count`, `overlap_count`,
`signal_count`, `same_changed_symbol_count`, `cross_pr_require_critic_count`,
`cross_pr_deepen_context_count` -- exactly mirrors
`TrajectoryIntelligenceSummary`'s shape. No rendered text, no PR
number, no author, no title anywhere in telemetry, matching every
prior package's own privacy discipline.

## 12. Persistence

No new table (mirrors O's and P's "re-derive live, never persist the
report itself" precedent). Six new nullable-default count columns on
`review_runs` via migration `0025_cross_pr_intelligence`:
`cross_pr_peer_count`, `cross_pr_overlap_count`, `cross_pr_signal_count`,
`cross_pr_same_changed_symbol_count`, `cross_pr_require_critic_count`,
`cross_pr_deepen_context_count`.

## 13. No new ancestry threading needed (unlike Milestone P)

Trajectory Intelligence needed `previous_generation_ancestry_verified`
threaded through the whole call chain because it reasons about *this
run's own* lineage continuity. Cross-PR Intelligence reasons about
*other* PRs' independently-persisted state -- peer eligibility (open,
non-stale head) is entirely self-contained inside
`patchfrog/cross_pr_intelligence/queries.py`, decided fresh from
already-persisted data every call. No new parameter needs to be added
to `review_local`/`review_pull_request`/`_run`'s public signatures.

## 14. 30-scenario corpus (minimum), real DB-backed

`tests/integration/test_cross_pr_intelligence_corpus.py` -- staged via
the real repositories (`PullRequestRepository`, `ReviewGenerationRepository`,
`ReviewCandidateModel`), never a hand-built `CrossPRPeer` standing in
for a real round trip. Covers (non-exhaustive list; full enumeration
in the corpus file itself): no peers exist; one open peer with a real
`SAME_CHANGED_SYMBOL` overlap; one peer changing a *different* symbol
in the *same file* (must NOT overlap -- explicit negative case per
spec); a closed peer (excluded); a merged peer (excluded); a peer whose
`head_sha` no longer matches its latest reviewed generation's
`commit_sha` (stale, excluded); a peer in a *different* repository
(hard filter, excluded even with an identical symbol name); more than
`MAX_CROSS_PR_PEERS` open peers (bounded, most-recently-active kept);
a peer with more than `MAX_CROSS_PR_SURFACES_PER_PEER` changed
surfaces (bounded); multiple peers overlapping the same current-PR
surface (deduplicated into one signal); `pull_request_id=None` (local
review, empty report); a peer whose only match is a module-region
candidate (`qualified_name=None`, excluded).

## 15. Docs / validation artifacts

`docs/cross-pr-intelligence.md`, `validation/cross_pr_intelligence/README.md`.

## 16. Explicitly not started

Cross-Repo Intelligence, per the standing instruction. Implementation
proceeds now that this audit is complete.
