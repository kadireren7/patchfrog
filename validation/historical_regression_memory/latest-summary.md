# Historical Regression Memory Foundation — Audit & Validation

Milestone N. Deterministic detection of "has this repository already
learned something painful about this surface, and is the current PR
re-entering that risk?" -- using only PatchFrog's own trusted historical
review outcomes, never git-history mining, blame, churn analytics,
semantic search, or an LLM-generated memory. Extends
[[patchfrog_change_intelligence_foundation]] (J),
[[patchfrog_contract_blast_radius_intelligence]] (K),
[[patchfrog_intent_verification_foundation]] (L), and
[[patchfrog_test_intelligence_foundation]] (M).

**PatchFrog does not treat repeated change or code churn as evidence of
a regression.** Historical Regression Memory only uses trusted
historical review outcomes tied to concrete repository surfaces.

## 1. Audit (written before any implementation)

### What historical finding data is persisted today?

`AIFindingModel` (`ai_findings`) is the durable record of every finding
that survived validation/critic/dedup for one review run:
`review_run_id`, `candidate_id` (FK to `ReviewCandidateModel`),
`file_path`, `category` (`FindingCategory`), `severity`, `created_at`.
`ReviewCandidateModel` (`review_candidates`) carries `file_path`,
`symbol_id` (FK to `SymbolModel`, **not stable across re-indexing --
see below**), `symbol_name`, `qualified_name`. `ReviewRunModel`
(`review_runs`) carries `repository_id`, `commit_sha`,
`pull_request_id`. Joining `ai_findings -> review_candidates ->
review_runs` gives every field needed to identify *where* a historical
finding lived, with zero new tables.

### Can a prior finding be tied to symbol/file/evidence surface?

Yes, via the join above -- `file_path` + `qualified_name` (both plain,
stable strings). **`symbol_id` cannot be used**: `SymbolModel.id` is
`default=uuid.uuid4`, freshly assigned every time a symbol row is
created within one `repository_index_id`. Every new indexing run
creates an entirely new `RepositoryIndexModel` with entirely new
`SymbolModel` rows -- a historical `symbol_id` and a current
`symbol_id` for the *same* function are different, unrelated UUIDs.
Identity across historical and current review runs must use
`(file_path, qualified_name)`, never UUID equality across indexes (spec
section 6's exact concern, confirmed true).

### Do finding ids survive lifecycle feedback? What does FIXED/USEFUL mean today?

Phase 9's `ExplicitCommand` enum (`patchfrog.feedback.domain`) defines
exactly four developer-issued `/patchfrog <token>` reply commands:
`useful`, `false-positive`, `fixed`, `ignore`. Each becomes a
`FeedbackEventType.EXPLICIT_COMMAND` event tied to a `finding_id`.
`patchfrog.feedback.assessment.compute_finding_assessment` derives a
`FindingFeedbackSummary` with plain counts
(`explicit_useful`/`explicit_false_positive`/`explicit_fixed`/
`explicit_ignore`) and a `FeedbackAssessment`
(`usefulness_signal`/`correctness_signal`, both `SignalPolarity`).
Critically: **`explicit_fixed` moves `correctness_signal` to POSITIVE
directly** ("explicit /patchfrog fixed command") -- the single
strongest, least ambiguous signal in the whole feedback system, a
developer explicitly confirming a real bug existed and was fixed.
`explicit_useful` moves `usefulness_signal` to POSITIVE and is the
first (strongest) branch of `is_high_value_candidate`. These are
persisted, queryable, plain integer columns on `FeedbackAssessmentModel`
(`feedback_assessments`, keyed by `(finding_id, assessment_version)`).

**This module's own core principle is a hard constraint on this
milestone**: "feedback is noisy evidence, not ground truth." Reactions
alone, thread-resolution alone, and `finding_disappeared` alone are
explicitly *not* used to move `correctness_signal`. Historical
Regression Memory's trust model must respect this -- see section 2.

### Can false-positive feedback exclude evidence safely?

Yes -- `explicit_false_positive` and `explicit_ignore` are plain,
persisted counts. `is_false_positive_candidate` already treats
`explicit_false_positive > 0` as the strongest available negative
signal. Mandatory exclusion (spec section 11) is therefore a simple,
sound SQL predicate: `explicit_false_positive = 0 AND explicit_ignore
= 0`.

### Is there enough data for same-symbol / same-file recurrence?

Yes for same-symbol (exact `(file_path, qualified_name)` match, see
above). Same-file-alone recurrence is likewise directly queryable, but
**a correction round concluded it is too weak to ever independently
seed a candidate** -- see section 5 and the correction narrative in
section 13b: v1 never constructs a bare same-file match at all
(precision over taxonomy coverage).

### Can related-surface recurrence use the repository graph safely?

Yes, but **without any new graph traversal**: J's `ChangeUnit.affected_surface`,
K's `ContractDelta.blast_radius`, and L's `PotentialIntentGap.expected_surface`
are all already-computed `AffectedSymbolRef` objects (`file_path`,
`qualified_name`, `relation`, `distance`) from the *current* review run.
Matching a historical record's `(file_path, qualified_name)` against
these already-computed nodes answers "is this historical surface
graph-connected to today's change" with zero new `RepositoryQueryService`
calls -- reusing J/K/L exactly as spec section 4 requires.

### What cannot be proven and must be deferred?

- **Rename/move continuity across historical distance.** Phase 7's
  own `patchfrog.review_memory.symbol_continuity` already solves
  rename/move detection, but only between two *adjacent* index
  versions within one PR's own incremental-review chain, using each
  symbol's `content_hash` -- content hashes for a historical symbol
  from months/PRs ago are not retained anywhere accessible for this
  purpose. Reusing that machinery across arbitrary historical distance
  would require data this system does not keep. **DEFERRED**: v1
  supports exact `(file_path, qualified_name)` identity only; a symbol
  renamed or moved since its historical finding is invisible to this
  milestone (documented limitation, never guessed at).
- **Cross-repository / cross-fork memory.** Every query is scoped by
  `repository_id` (a hard `WHERE` clause, never optional) -- see
  section 26. Fork lineage is not modeled anywhere in
  `RepositoryModel`; deferred, never inferred.
- **A genuinely new graph traversal for "related surface."** Not
  needed at all -- section above.

### How does this reuse Review Memory instead of a parallel history DB?

It doesn't touch Phase 7's `review_memory_findings`/
`review_memory_transitions` tables at all (those are PR-scoped
incremental-review carry-forward state, a different concern). Instead
it reuses Phase 9's raw `feedback_events` (trust, computed point-in-time
-- see the correction in section 13b for why the *persisted*
`feedback_assessments` snapshot is the wrong source) joined with the
existing `ai_findings`/`review_candidates`/`review_runs` chain (surface
identity + repository/commit anchoring) -- **zero new tables**. The
only new persistence is five nullable-default summary columns on
`review_runs` (migration `0022_historical_regression`), exactly
mirroring J/K/L/M's own `change_story`-adjacent persistence pattern,
needed because publication runs as a separate, independently-retriable
Celery task from review generation.

## 2. Historical evidence trust model

Only two states are backed by real, unambiguous persisted facts --
**not** the four-state sketch (`CONFIRMED_FIXED`/`CONFIRMED_USEFUL`/
`REVIEW_ACCEPTED`/`WEAK`) offered as a starting point; `REVIEW_ACCEPTED`
and `WEAK` have no corresponding concrete signal in this codebase and
are not invented:

```
class HistoricalEvidenceStrength(StrEnum):
    CONFIRMED_FIXED = "confirmed_fixed"    # explicit_fixed > 0
    CONFIRMED_USEFUL = "confirmed_useful"  # explicit_useful > 0
```

**Eligibility (fail-closed)**: a historical finding is a candidate
seed **only if**:

```
(explicit_fixed > 0 OR explicit_useful > 0)
AND explicit_false_positive == 0
AND explicit_ignore == 0
```

Both exclusions are unconditional -- even a finding with `explicit_fixed
> 0` is excluded if `explicit_false_positive > 0` was *ever* also
recorded (append-only history; conservative "fail closed" per spec,
rather than adjudicating which command "wins"). A finding with
*neither* trust signal (only reactions, thread state, or nothing at
all) never seeds memory -- it simply never has a
`HistoricalRegressionRecord` at all, exactly like a finding with zero
feedback never gets a `feedback_assessments` row in the first place.

## 3. `HistoricalRegressionRecord`

```
HistoricalRegressionRecord:
    historical_finding_id, repository_id, historical_review_run_id,
    historical_commit_sha, source_file_path, source_qualified_name,
    finding_category, evidence_strength, bounded_evidence_fingerprint,
    observed_at
```

`bounded_evidence_fingerprint` is a short, already-bounded string (the
finding's own `title`, truncated -- never `message`/`reasoning_summary`/
`suggested_fix`/`impact`/raw evidence quotes). No raw source body, no
hidden reasoning, no full historical finding prose. `observed_at`
holds the point-in-time-computed `trusted_at` (the earliest qualifying
`fixed`/`useful` event's own `occurred_at`, per section 9/13b) -- not
the finding's `created_at` and not the assessment row's `computed_at`.

## 4. Current surface pool (reused, not re-derived)

Built once per review run from data J/K/L already computed:

1. Every `ChangeUnit.changed_candidates` member -- `is_directly_changed=True`.
2. Every `ChangeUnit.affected_surface` node with a resolved
   `qualified_name` -- `is_directly_changed=False`.
3. Every `ContractDelta.blast_radius` node with a resolved
   `qualified_name` -- `is_directly_changed=False`.
4. Every `PotentialIntentGap.expected_surface` -- `is_directly_changed=False`.

`PotentialTestGap` is deliberately **not** a match-kind source (its
`source_qualified_name` is frequently just a file-path placeholder for
`TEST_TOUCHED_BUT_WEAKENED`, not a real resolved symbol) -- instead, a
test gap on the *same* `(file_path, qualified_name)` a match was
already found through is folded into that match's evidence text as
enrichment, never a separate match path.

## 5. Match kind hierarchy (implemented exactly as specified, no embeddings)

1. **`SAME_SYMBOL`**: historical `(file_path, qualified_name)` exactly
   equals a pool entry with `is_directly_changed=True` -- the exact
   symbol that produced a trusted historical finding is being edited
   again.
2. **`SAME_QUALIFIED_NAME_IN_SAME_FILE`**: exact match against a pool
   entry with `is_directly_changed=False` whose `file_path` is also one
   of the files directly touched this PR -- the historical symbol is
   present (as context/affected, not itself edited) in a file that
   *is* being changed.
3. **`GRAPH_RELATED_SURFACE`**: exact match against a pool entry with
   `is_directly_changed=False` whose `file_path` is **not** one of the
   files directly touched this PR -- reached purely through J/K/L's own
   real graph relation (a call edge, a blast-radius edge, an intent-gap
   surface), never a new traversal.

**`SAME_FILE` is never constructed in v1** -- see the correction round
in section 13b. It remains defined on `HistoricalMatchKind` (and
`PREVIOUS_FIXED_FINDING_SAME_FILE` on `HistoricalRegressionReasonCode`)
for forward documentation only. Every real match above requires an
*exact* `(file_path, qualified_name)` hit against the current surface
pool -- a file matching alone, with no symbol identity confirmed, is
never enough (spec's own correction: "the safe fallback is: SAME_FILE
-> no candidate").

No embeddings, no fuzzy matching, no NLP over old finding prose
anywhere in this hierarchy.

## 6. Reason code mapping (spec's own 4-item taxonomy, no more)

| Match kind | Trust | Reason code |
|---|---|---|
| SAME_SYMBOL / SAME_QUALIFIED_NAME_IN_SAME_FILE | CONFIRMED_FIXED | `PREVIOUS_FIXED_FINDING_SAME_SYMBOL` |
| SAME_SYMBOL / SAME_QUALIFIED_NAME_IN_SAME_FILE | CONFIRMED_USEFUL | `PREVIOUS_USEFUL_FINDING_SAME_SYMBOL` |
| GRAPH_RELATED_SURFACE | either | `PREVIOUS_REGRESSION_RELATED_SURFACE` |

`PREVIOUS_FIXED_FINDING_SAME_FILE` has no live mapping -- it is never
produced (see above).

## 7. Dedup ownership vs J/K/L/M (spec section 16)

When a matched current surface node is *also* already a `MISSING`
`ExpectedCompanionChange` (J's `CALLER_NOT_UPDATED`/`TEST_NOT_UPDATED`,
or K's `CONTRACT_CONSUMER_NOT_UPDATED`), an already-mapped
`PotentialIntentGap` (L), or a `PotentialTestGap` (M) on the *same*
`(file_path, qualified_name)`, the `PotentialHistoricalRegression`
carries a reference to that existing object
(`enriches_companion`/`enriches_intent_gap`/`enriches_test_gap`,
whichever applies, `None` otherwise) -- it never becomes a second,
competing top-level warning about the same surface. When the matched
surface has no existing J/K/L/M candidate at all, the
`PotentialHistoricalRegression` stands alone (`enriches_* = None`).

## 8. Repository isolation

Every historical query is scoped by a mandatory `repository_id`
equality filter -- there is no code path that can compare across
repositories. Same `qualified_name` in a different repository never
matches (see the corpus's mandatory isolation test).

## 9. Temporal leakage protection (corrected -- see section 13b)

**Point-in-time, not "current state."** Trust is computed strictly
from `feedback_events` rows with `occurred_at <= as_of` -- never from
the persisted `feedback_assessments` snapshot, which reflects trust
*now* (whenever the row was last recomputed), not trust as of the
current review's own temporal boundary. `as_of` is always the current
review run's own persisted `started_at` (see
`patchfrog.review.service`'s integration point) -- reproducible for a
given review run, never a fresh wall-clock read that would differ
across retries or a later backfill/replay of the same historical
point.

The controlled corpus proves this two ways: (1) the original
before/after-trust-exists case (T1 finding exists with zero feedback
rows at all; T2 a real feedback event + recompute; query before vs.
after), and (2) the *true* temporal-leakage proof added during
correction (T1 finding exists; T2 a current review's `as_of` boundary
is captured and evaluated -- zero candidates; T3, strictly after T2, a
real `/patchfrog fixed` event is recorded; **replaying the exact same
T2 `as_of` boundary again** -- still zero candidates, proving a row
that now exists but is dated after the boundary does not leak
backwards; only a genuinely later `as_of`, captured after T3, sees it).
Never a hand-constructed `HistoricalRegressionRecord` standing in for
what should be a real DB round trip.

## 10. Query bounds

- `MAX_HISTORICAL_LOOKBACK_ROWS` -- the single bounded SQL query (one
  per review run) groups `feedback_events` rows
  (`event_type=EXPLICIT_COMMAND`, `repository_id = ?`,
  `occurred_at <= as_of`) by `finding_id`, computing
  `fixed_count`/`useful_count`/`false_positive_count`/`ignore_count`/
  `trusted_at` (`MIN(occurred_at)` over qualifying rows) via portable
  `SUM(CASE ...)`/`MIN(CASE ...)` aggregation (no dialect-specific
  `FILTER` clause), then a `HAVING` clause applies the eligibility
  predicate before ever joining to `ai_findings`/`review_candidates`/
  `review_runs`. Ordered by strength then most-recently-trusted then
  id; never returns more than this many rows -- no per-surface query
  loop, no N+1.
- `MAX_HISTORICAL_RECORDS_PER_SURFACE` -- at most this many historical
  records considered per matched current surface.
- `MAX_HISTORICAL_REGRESSION_CANDIDATES` -- bounds the final candidate
  list per run, mirroring `MAX_TEST_GAPS_PER_UNIT`'s own role in M.

## 11. Architecture: the one new I/O this milestone needs

Unlike M (fully session-free), this milestone's *query* layer
(`patchfrog.historical_regression_memory.queries`) is necessarily
async/session-based -- historical evidence lives in the database, not
in this run's own in-memory objects. The *matching* layer
(`patchfrog.historical_regression_memory.matching`) is pure/synchronous,
exactly like J/K/L/M's own candidate-derivation code -- it only ever
consumes already-fetched `HistoricalRegressionRecord`s and
already-computed J/K/L surface objects. Zero new graph queries beyond
the one bounded trust query; zero LLM calls anywhere in the package.

## 12. Persistence decision

No new table. Migration `0022_historical_regression` adds five
nullable-default columns to `review_runs`:
`historical_trusted_record_count`, `historical_match_kind_counts`,
`historical_regression_candidate_count`,
`historical_summary_rendered`, `historical_summary_text` -- the exact
same pattern J/K/L/M established, needed only because publication is a
separate Celery task from review generation.

## 13a. Self-caught bug during corpus authoring: test-only PRs could leak a production regression via a real call edge

Building the mandatory "test-only PR" negative corpus case exposed a
real design gap: a test file that merely *calls* a historically-risky
production symbol (e.g. `test_apply_discount` calling `apply_discount`)
produces a completely real, legitimate J call edge -- so
`derive_affected_surface` correctly includes `apply_discount` in that
ChangeUnit's `affected_surface`, and the matching layer correctly
found it there and produced a `GRAPH_RELATED_SURFACE` candidate. This
is technically sound graph traversal, but it violates the "not an
inverse feature detector" hard invariant (spec section 5's own
concern, and the same failure mode Milestone M's own corpus caught for
its analogous test-only requirement): nothing about production risk
actually changed in a test-only PR.

Fixed by excluding every `TEST`-kind `ChangeUnit` (one whose every
`changed_candidates` member lives in a test file, per
`classify_candidate`/`combine_kinds`) from the surface pool entirely --
neither its `changed_candidates` nor its `affected_surface` ever
contributes a match, mirroring Milestone M's own `NO_TEST_SURFACE_FOUND`
BEHAVIOR-only conservatism. A second, smaller fixture bug was caught
alongside it while building the "stale candidate disappears on new
exact head" case: computing head B's diff against the *original*,
distant `base_sha` (rather than head A's own commit) meant head A's
already-committed change to the risky symbol was still part of head
B's diff, so the candidate never actually disappeared -- fixed by
diffing head B against head A's own SHA, correctly modeling "a fresh,
independent PR starts from here," not a cumulative diff against
ancient history.

## 13b. External-review correction round: SAME_FILE was too permissive, and temporal isolation was incomplete

Two real semantic gaps were found by external review before merge,
both fixed:

**1. `SAME_FILE` independently created a candidate for a genuinely
unrelated symbol.** The original design let a `CONFIRMED_FIXED`
historical finding on symbol A become a candidate whenever the *file*
containing it was touched this PR, even when the actual edit was to a
completely different symbol B in the same file (e.g. a historical bug
in `apply_tax` "recurring" merely because `apply_discount`, an
unrelated function in the same file, changed). This violates the
spec's own "same file alone is weak... otherwise defer" requirement.
Fixed by removing `SAME_FILE` from ever being constructed at all (spec
section 3's explicit fallback: "the safe fallback is: SAME_FILE -> no
candidate") -- `_match_kind_for` now returns `None` whenever no *exact*
`(file_path, qualified_name)` match exists in the current surface
pool, full stop. `HistoricalMatchKind.SAME_FILE`/
`HistoricalRegressionReasonCode.PREVIOUS_FIXED_FINDING_SAME_FILE`
remain on their enums for forward documentation, never produced. This
also automatically fixed a second, related concern (rename/move
falling back to `SAME_FILE` noise): with `SAME_FILE` gone entirely, a
renamed symbol now correctly produces zero candidates rather than a
weaker-but-still-fabricated fallback.

**2. The temporal model checked `finding.created_at`/row-existence,
never the actual moment trust was established.** The original query
joined the *persisted* `feedback_assessments` snapshot -- a table that
reflects trust *as of whenever it was last recomputed*, not as of any
particular current review's own point in time. This under-proves
temporal isolation: it correctly hides a finding with *no* feedback
row at all yet, but says nothing about whether a row that *does*
exist, dated after a given review's own boundary, would incorrectly
leak into a replay of that earlier review (e.g. a backfill run).
Fixed by re-deriving trust directly from raw `feedback_events` rows
filtered to `occurred_at <= as_of` in the bounded SQL query itself
(see section 10), where `as_of` is threaded from the real review
pipeline's own persisted `ReviewRunModel.started_at` (never a fresh
wall-clock read -- reproducible for a given review run, see
`patchfrog.review.service`'s integration point). A new, stronger
corpus case (`test_case_true_temporal_leakage_replay_never_sees_future_trust`)
proves this directly: captures a review boundary, confirms zero
candidates, records a real `/patchfrog fixed` event *after* that
boundary, replays the *exact same* boundary and confirms it is still
zero, then confirms a genuinely later boundary does see it.

## 13. What is explicitly out of scope / deferred (never faked)

- Rename/move historical continuity (see above).
- Cross-repository / cross-fork memory (see above).
- A numeric regression-probability score of any kind.
- A "Repository history" giant summary section, or any count like "N
  past bugs touched this file."
- A Historical Agent -- Correctness/Security/Critic remain the only
  authoritative roles; this package only ever adds bounded evidence
  text.
- Matching solely on `FindingCategory` (never a substitute for surface
  evidence, per spec section 29).

## 14. Corpus results (21 behavioral scenarios, post-correction)

All cases stage real T1 (historical finding persisted via real
`ReviewRunModel`/`ReviewCandidateModel`/`AIFindingProposalModel`/
`AIFindingModel` rows)/T2 (real `FeedbackEventModel` +
`recompute_and_persist_all`)/T3 (a real, independent current review
built via real indexing/diffing/Change-Contract Intelligence) steps.
Zero FakeLLM-authored ground truth for the historical side; zero
hand-constructed `HistoricalRegressionRecord` standing in for the real
DB round trip.

| # | Case | Result |
|---|------|--------|
| 1 | Prior FIXED finding, same symbol changed again | candidate, `SAME_SYMBOL` |
| 2 | Prior USEFUL finding, same symbol, current relevance | candidate, `CONFIRMED_USEFUL` |
| 3 | Prior finding later marked FALSE_POSITIVE (even alongside an earlier USEFUL) | 0 records, 0 candidates |
| 4 | Prior finding later marked IGNORE (even alongside an earlier FIXED) | 0 candidates |
| 5 | Same qualified name, different repository | 0 records, 0 candidates (isolation) |
| 6 | Same file, unrelated symbol (**corrected**) | 0 candidates -- `SAME_FILE` never fires |
| 7 | Historical record exists, current risky surface untouched | 0 candidates |
| 8 | Head A candidate exists; Head B (fresh incremental base) fixes the surface | candidate disappears |
| 9 | Old finding with zero feedback | 0 candidates (never recomputed, no trust row at all) |
| 10 | Temporal leakage: before any feedback row exists vs. after | invisible, then visible |
| 10a | **True temporal-leakage replay** (new): boundary captured, feedback recorded *after*, exact same boundary replayed | still invisible; only a later boundary sees it |
| 11 | Prior fixed finding on a contract consumer; real K stale consumer exists | enriches K's own candidate, no duplicate |
| 12 | Docs-only change | 0 candidates |
| 13 | Test-only PR (test calls a historically-risky production symbol) | 0 candidates (TEST-kind units excluded) |
| 14 | Historical SECURITY finding, same surface | candidate, same trust rules, no special weighting |
| 15 | 6 trusted findings on the same surface | bounded to `MAX_HISTORICAL_RECORDS_PER_SURFACE` (3) |
| 16 | 30 historical findings across the repository | bounded SQL fetch (`limit=10` respected exactly) |
| 17 | Symbol renamed since the historical finding (**corrected**) | 0 candidates -- no `SAME_SYMBOL`, no `SAME_FILE` fallback |
| 18 | Real `review_local` pipeline run | persisted correctly on `ReviewRunModel` |
| 19 | Telemetry/versioning round trip on a real report | version + counts correct |
| 20 | Structural: no `LLMProvider` import anywhere in the package | proven via AST scan |

## 15. Gates (final, post-correction)

- `ruff check .`: clean, whole repo.
- `mypy . --strict`: clean, whole repo.
- `pytest tests/`: full suite passing against real Postgres/Redis (see
  the final report for the exact total).
- Alembic: single head (`0022_historical_regression`), real upgrade
  applied cleanly on top of `0021_test_intelligence`.
- Both Docker images (`api`, `worker`) built clean.
- Secret scan (regex over the full staged diff): clean.
- `git diff --check`: clean, no whitespace errors.
- No Co-Authored-By trailer, no secret material, in the diff.

## 16. Versioning (final)

- `HISTORICAL_REGRESSION_MEMORY_VERSION = 1` (new).
- `REVIEW_PROMPT_VERSION`: 7 -> 8 (new `<historical_regression>` section).
- `TELEMETRY_SCHEMA_VERSION`: 5 -> 6 (new `historical_regression_memory` telemetry field).
- `REVIEW_POLICY_VERSION`/`REVIEW_ENGINE_VERSION`/`CONFIG_SCHEMA_VERSION`/
  `QUALITY_COST_POLICY_VERSION`/`CHANGE_INTELLIGENCE_VERSION`/
  `CONTRACT_INTELLIGENCE_VERSION`/`INTENT_VERIFICATION_VERSION`/
  `TEST_INTELLIGENCE_VERSION`: all unchanged, each pinned by
  `tests/unit/test_historical_regression_memory_versioning.py`.
