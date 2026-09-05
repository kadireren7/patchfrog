# Historical Regression Memory Foundation

`patchfrog/historical_regression_memory/` extends
`patchfrog/change_intelligence/` (Milestone J), `patchfrog/contract_intelligence/`
(Milestone K), `patchfrog/intent_verification/` (Milestone L), and
`patchfrog/test_intelligence/` (Milestone M) with a deterministic
answer to: **has this repository already learned something painful
about this surface, and is the current PR re-entering that risk?**

**PatchFrog does not treat repeated change or code churn as evidence
of a regression.** Historical Regression Memory only uses trusted
historical review outcomes tied to concrete repository surfaces. It is
explicitly **not**:

- generic git-history mining, blame analysis, or code-churn analytics,
- a fuzzy semantic search over every past PR,
- an LLM-generated repository memory,
- a new Historical Agent,
- a reputation/risk score of any kind.

## The trust model

Only two states are backed by real, unambiguous persisted facts:

- **`CONFIRMED_FIXED`** -- a developer explicitly replied
  `/patchfrog fixed` (Phase 9's `ExplicitCommand.FIXED`, the single
  strongest signal in the whole feedback system).
- **`CONFIRMED_USEFUL`** -- a developer explicitly replied
  `/patchfrog useful`.

A historical finding is eligible **only if** `(explicit_fixed > 0 OR
explicit_useful > 0) AND explicit_false_positive == 0 AND
explicit_ignore == 0` -- both exclusions unconditional, even alongside
an otherwise-trusted signal (append-only history; fail closed rather
than adjudicate which command "wins"). A finding with neither trust
signal simply never has a record at all.

## Lifecycle eligibility

Reactions, thread-resolution state, and `finding_disappeared` alone are
explicitly **never** used to establish trust here -- Phase 9's own core
principle ("feedback is noisy evidence, not ground truth") is a hard
constraint on this milestone, not a suggestion. Only the two explicit
commands above ever seed memory.

## Match kind hierarchy (no embeddings, no fuzzy matching)

1. **`SAME_SYMBOL`** -- the exact historical `(file_path,
   qualified_name)` is a symbol directly changed in the current PR.
2. **`SAME_QUALIFIED_NAME_IN_SAME_FILE`** -- the exact historical
   symbol is present (affected, not itself edited) in a file that
   *is* being changed this PR.
3. **`GRAPH_RELATED_SURFACE`** -- the exact historical symbol appears
   in the current review's own already-computed graph-connected
   surface (J's `affected_surface`, K's `blast_radius`, L's
   `expected_surface`) at a file that was **not** itself directly
   touched this PR -- reached purely through a real call/dependency
   edge, never a new traversal.

**`SAME_FILE` is never constructed in v1** -- a correction round found
that "the historical file was touched" alone is too weak (it let a
finding on symbol A "recur" merely because an unrelated symbol B in
the same file changed). It remains defined on `HistoricalMatchKind`
(and `PREVIOUS_FIXED_FINDING_SAME_FILE` on
`HistoricalRegressionReasonCode`) for forward documentation only.
Every real match requires an *exact* `(file_path, qualified_name)` hit
against the current surface pool.

**A `TEST`-kind `ChangeUnit` (every candidate in it lives in a test
file) never contributes anything to matching** -- neither its changed
candidates nor its affected surface. This was a real bug found while
building the corpus: a test file that merely *calls* a
historically-risky production symbol produces a completely real J
call edge, so without this exclusion a test-only PR could surface a
`GRAPH_RELATED_SURFACE` candidate about production code nothing in the
PR actually touched. Mirrors Milestone M's own `NO_TEST_SURFACE_FOUND`
BEHAVIOR-only conservatism.

## Reason codes (the spec's own 4-item taxonomy)

| Match kind | Trust | Reason code |
|---|---|---|
| SAME_SYMBOL / SAME_QUALIFIED_NAME_IN_SAME_FILE | CONFIRMED_FIXED | `PREVIOUS_FIXED_FINDING_SAME_SYMBOL` |
| SAME_SYMBOL / SAME_QUALIFIED_NAME_IN_SAME_FILE | CONFIRMED_USEFUL | `PREVIOUS_USEFUL_FINDING_SAME_SYMBOL` |
| GRAPH_RELATED_SURFACE | either | `PREVIOUS_REGRESSION_RELATED_SURFACE` |

`PREVIOUS_FIXED_FINDING_SAME_FILE` has no live mapping -- never produced.

## Same-symbol identity: why not `symbol_id`

`SymbolModel.id` is a fresh random UUID assigned every time a symbol
row is created within one repository index -- every new indexing run
creates entirely new symbol rows. A historical `symbol_id` and a
current `symbol_id` for the *same* function are unrelated UUIDs.
Identity across historical and current review runs is always
`(file_path, qualified_name)` -- stable plain strings, never a UUID
comparison across indexes.

## Renames/moves: deferred, not guessed

Phase 7's own `patchfrog.review_memory.symbol_continuity` already
solves rename/move detection via `content_hash`, but only between two
*adjacent* index versions within one PR's own incremental-review
chain -- content hashes for a historical symbol from an arbitrary
distance back are not retained anywhere accessible for this purpose.
A symbol renamed or moved since its historical finding is invisible to
this milestone -- produces zero candidates (no `SAME_SYMBOL` match, and
no `SAME_FILE` fallback either, since that tier is never constructed at
all -- see the match hierarchy above) -- documented, never guessed at.

## J/K/L/M integration and dedup ownership

The current surface pool reuses, never re-derives: `ChangeUnit.changed_candidates`/
`affected_surface` (J), `ContractDelta.blast_radius` (K), and
`PotentialIntentGap.expected_surface` (L). `PotentialTestGap` (M) is
deliberately not a match-kind source (its own `source_qualified_name`
is often just a placeholder for `TEST_TOUCHED_BUT_WEAKENED`) -- it only
ever participates as *enrichment*.

**Dedup ownership**: when the matched current surface is already a
real `MISSING` `ExpectedCompanionChange` (J's `CALLER_NOT_UPDATED`/
`TEST_NOT_UPDATED`, or K's `CONTRACT_CONSUMER_NOT_UPDATED`), an already
-mapped `PotentialIntentGap` (L), or a `PotentialTestGap` (M) on the
same `(file_path, qualified_name)`, the `PotentialHistoricalRegression`
references that existing object (`enriches_companion`/
`enriches_intent_gap`/`enriches_test_gap`) instead of becoming a
second, competing top-level warning. When the matched surface has no
existing J/K/L/M candidate at all, it stands alone.

## Repository isolation

Every historical query is scoped by a mandatory `repository_id`
equality filter in the one bounded SQL query this milestone issues --
there is no code path that compares across repositories. Same
`qualified_name` in a different repository never matches.

## Temporal leakage protection

Trust is computed **point-in-time**, not from current state: the query
reads raw `feedback_events` rows with `occurred_at <= as_of` only --
never the persisted `feedback_assessments` snapshot, which reflects
trust *now*, whenever it was last recomputed, not as of any particular
review's own boundary. `as_of` is always the current review run's own
persisted `started_at` -- reproducible for a given review run, never a
fresh wall-clock read.

The controlled corpus proves this with a real replay: a review
boundary is captured, a real `/patchfrog fixed` event is then recorded
*strictly after* that boundary, and replaying the *exact same* boundary
again still produces zero candidates -- the row now exists in the
database but is correctly excluded because it is dated after the
point being evaluated. Only a genuinely later boundary, captured after
the event, sees it. Never a hand-constructed `HistoricalRegressionRecord`
standing in for what should be a real round trip.

## Incremental / exact-head semantics

Historical candidates are recomputed from scratch against the current
exact head every time, never carried forward from a previous head. If
the current PR's own diff no longer touches the historically-risky
surface (directly or via a real graph connection), the candidate
disappears; if a later PR reintroduces the risk, it reappears.

## Query bounds

- `MAX_HISTORICAL_LOOKBACK_ROWS` (200) -- the one bounded SQL query per
  review run (a portable `SUM(CASE ...)`/`MIN(CASE ...)` aggregation
  over `feedback_events` grouped by `finding_id`, `HAVING` the trust
  predicate, joined to `ai_findings`/`review_candidates`/`review_runs`)
  never returns more rows than this; no per-surface query loop, no N+1.
- `MAX_HISTORICAL_RECORDS_PER_SURFACE` (3) -- at most this many
  historical records considered per matched current surface.
- `MAX_HISTORICAL_REGRESSION_CANDIDATES` (10) -- bounds the final
  candidate list per run.

Ordering is always strongest-trust-first (`CONFIRMED_FIXED` before
`CONFIRMED_USEFUL`), then most recent, then a stable id tie-break.

## Architecture

Unlike M (fully session-free), this milestone's query layer
(`patchfrog.historical_regression_memory.queries`) is necessarily
async/session-based -- historical evidence lives in the database. The
matching layer (`patchfrog.historical_regression_memory.matching`) is
pure/synchronous, exactly like every other Intelligence package's own
candidate-derivation code. Zero new repository-graph queries beyond
the one bounded trust query; zero LLM calls anywhere in the package
(structurally proven -- no `LLMProvider` import anywhere).

## Persistence

No new table -- the one bounded query reuses Phase 9's own raw
`feedback_events` joined with the existing
`ai_findings`/`review_candidates`/`review_runs` chain. `review_runs`
gained five nullable-default columns (migration
`0022_historical_regression`): `historical_trusted_record_count`,
`historical_match_kind_counts`, `historical_regression_candidate_count`,
`historical_summary_rendered`, `historical_summary_text` -- the same
pattern J/K/L/M established, needed only because publication is a
separate Celery task from review generation.

## Review pipeline integration

Computed once per run, right after Test Intelligence (consuming
Change/Contract/Intent/Test Intelligence's already-built evidence plus
the one bounded trust query). A fifth optional
`<historical_regression>` prompt section (`REVIEW_PROMPT_VERSION`
7 -> 8), attached only to the exact candidate that matches a real,
trusted historical finding. `REVIEW_POLICY_VERSION`/
`REVIEW_ENGINE_VERSION`/every prior Intelligence package's own version
are **not** bumped. **No new agent role** -- Correctness, Security, and
Critic remain the only authoritative roles; this package only ever
adds bounded evidence.

## Change Story and conditional summary

`build_historical_story_prefix` produces at most one bounded sentence
("Historical context: ... previously had a trusted, resolved
finding."), only for the two strongest match tiers
(`SAME_SYMBOL`/`SAME_QUALIFIED_NAME_IN_SAME_FILE`) -- never for a
`GRAPH_RELATED_SURFACE` match alone, and never for every PR
with any historical finding anywhere in the repository. The conditional
`### Historical context` publication block uses the same eligibility
bar. Neither ever renders a count ("N past bugs touched this file") or
a score.

## Limitations

- Rename/move continuity is deferred (see above).
- Cross-repository / cross-fork memory is deferred -- fork lineage is
  not modeled anywhere in `RepositoryModel`.
- No per-branch/path-level historical correlation -- matching is at
  file/symbol granularity only.
- `SAME_FILE` is never constructed at all in v1 (see the match
  hierarchy above) -- a real design correction, not merely an
  unimplemented tier.
- Historical-regression candidates are heuristic evidence, not proof --
  they must survive the existing reviewer/critic pipeline like any
  other finding before ever reaching GitHub.
