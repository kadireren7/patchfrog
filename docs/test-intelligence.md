# Test Intelligence Foundation

`patchfrog/test_intelligence/` extends `patchfrog/change_intelligence/`
(Milestone J), `patchfrog/contract_intelligence/` (Milestone K), and
`patchfrog/intent_verification/` (Milestone L) with a deterministic
answer to a narrower question: **when behavior changed, is there a
correct behavioral test surface for it?** It is explicitly **not**:

- a coverage-percentage product ("87% covered"),
- a test-generation agent (it never writes or suggests test code),
- a mutation-testing system,
- an LLM-only classifier of "good" vs. "bad" tests.

Every signal here is structural and evidence-backed -- never a
semantic judgment about test *quality*, only about test *existence*
and gross structural *erosion*.

## The two signals

Both trace back to real, already-available evidence -- never a new
repository-graph query, never a new I/O primitive (see "Architecture"
below).

### `NO_TEST_SURFACE_FOUND`

A `ChangeUnit` whose combined `change_kind` is exactly `BEHAVIOR`
(never `CONTRACT`/`CONFIGURATION`/`PERSISTENCE`/`INFRASTRUCTURE`/`TEST`/
`MIXED` -- see "Scope restriction" below) has a changed, symbol-resolved
file with **zero** discoverable test file at all. This is distinct
from, and never overlaps with, Change Intelligence's own
`CompanionReasonCode.TEST_NOT_UPDATED`: J's signal only ever fires when
a likely test file (a real `FILE_TESTS_FILE` graph edge) *exists* and
wasn't touched. This milestone's signal fires in the complementary
case -- no such edge was ever found at all.

### `TEST_TOUCHED_BUT_WEAKENED`

A file matching `patchfrog.indexing.inventory.is_test_path` that was
genuinely touched in this diff shows a structural erosion signal:

- **net assertion-marker count decreased** -- lines matching `assert `,
  `self.assert*(`, `pytest.raises(`, `with raises(` counted in
  `deleted_lines` vs. `added_lines`; only a strictly negative net
  flags (unchanged, or assertions-only-added, never flags); or
- **a skip/xfail marker was newly added** -- `@pytest.mark.skip`,
  `@pytest.mark.xfail`, `pytest.skip(`, `pytest.importorskip(`; only a
  strictly positive net flags -- **removing** a skip marker
  (un-skipping a test) has a negative net and correctly never flags,
  since that is strengthening, not weakening.

Both counts come from the diff itself (`DiffFile.added_lines`/
`deleted_lines`) -- regex-only, never NLP, never a judgment about
whether the *remaining* assertions are meaningful.

## Reuse, not duplication

Nothing here re-derives what changed, what it affects, or what already
has a test link:

- **Dedup against J**: a changed file is only ever eligible for
  `NO_TEST_SURFACE_FOUND` when **no** `ExpectedCompanionChange` with
  `reason_code=TEST_NOT_UPDATED` and matching `source_file_path` exists
  among the companions J (and K's stale consumers, passed through the
  same combined list) already produced -- checked regardless of that
  companion's own `status`. Even a `MISSING` `TEST_NOT_UPDATED`
  companion means "a test file *was* found" -- J's territory, never
  re-flagged here. See `patchfrog.test_intelligence.expectations.derive_test_surfaces`.
- **No overlap with K**: a function with a real cross-file caller is
  classified `ChangeKind.CONTRACT`, not `BEHAVIOR` --
  `NO_TEST_SURFACE_FOUND`'s BEHAVIOR-only scope means K's own
  stale-consumer signal space (a missing/stale *consumer*) and this
  milestone's signal space (a missing *test*) never compete for the
  same node.
- **No overlap with L**: `PotentialIntentGap` only ever fires from
  explicit PR title/body text; this milestone's signals never look at
  PR metadata at all. The two candidate kinds can coexist for the same
  `ChangeUnit` without duplicating each other -- proven directly in the
  corpus.
- **No lexical/mapping module**: unlike L, neither signal here compares
  prose to a graph object -- `NO_TEST_SURFACE_FOUND` is a pure
  existence check over already-attributed objects (same `file_path`),
  and `TEST_TOUCHED_BUT_WEAKENED` is anchored to a companion-confirmed
  correlation (see below), then a pure line-count comparison within
  that one file's own diff. Introducing bounded lexical matching here
  would be unused machinery, not future-proofing.

## Test-only PRs stay quiet (not an inverse feature detector)

`TEST_TOUCHED_BUT_WEAKENED` is **anchored to a real, same-PR production
change** -- it is never evaluated for an arbitrary touched test file. A
test file is only ever eligible when J's own companions already
confirm, via a real `ExpectedCompanionChange` with
`reason_code=TEST_NOT_UPDATED` naming it as `expected_file_path`, that
it is linked to a changed *production* file. Whether the test file was
itself genuinely touched is then answered independently -- and more
precisely than J's own `status` field -- by direct membership in
`diff_files` (see the note on `status` below).

This is sound, not a heuristic guess, because of how J's own companions
are constructed: `patchfrog.change_intelligence.companions._test_staleness`
iterates the *production* side only -- for each changed production
candidate, it looks up that candidate's own linked test files. It never
runs in the reverse direction (starting from a changed test file and
asking what it tests). A PR that touches only test files therefore
produces **zero** `TEST_NOT_UPDATED` companions of any status, for any
file -- so `TEST_TOUCHED_BUT_WEAKENED` structurally cannot fire without
a real production change in the same PR. `NO_TEST_SURFACE_FOUND` is
unaffected by this concern in the other direction: it already requires
`ChangeKind.BEHAVIOR`, which by construction excludes test-only units.

Put plainly: **a test-only PR is never independently judged by Test
Intelligence.** It may still be reviewed by PatchFrog's normal
reviewer/static layers, but this package produces no behavioral
test-gap candidate for it -- the premise ("does the test surface verify
the behavior that changed?") does not hold when nothing behavioral
changed.

## Scope restriction: BEHAVIOR-kind-only for `NO_TEST_SURFACE_FOUND`

`ChangeUnit.change_kind` is `combine_kinds` over every constituent
candidate's own classification -- it is exactly one non-`MIXED` value
only when *every* candidate in the unit agrees. A `BEHAVIOR`-kind unit
is therefore guaranteed, by construction, to contain no test file, no
config/infra/persistence-path file, and no symbol with a real
cross-file caller. This is the narrowest, most honest scope for "a
genuinely new/changed piece of business logic with nothing else going
on" -- deliberately excluding `MIXED` units (which may contain an
untested `BEHAVIOR` candidate alongside, say, a `CONFIGURATION`
candidate) for this first version. Widening to per-candidate (rather
than per-unit) classification inside `MIXED` units is a natural,
safe follow-up, deferred rather than risking a broader, less-audited
first cut.

## Architecture: zero new I/O

Both signals are fully computable from data every review run already
builds before this package would run:

- `NO_TEST_SURFACE_FOUND` needs only `ChangeUnit.change_kind`/
  `changed_candidates` and the combined `expected_companions` list
  (already computed once per run by J/K).
- `TEST_TOUCHED_BUT_WEAKENED` needs only the PR's own already-parsed
  `DiffFile`s -- no base-commit fetch (unlike K), no repository-graph
  query (unlike J/K).

`patchfrog.test_intelligence` is therefore, like L, entirely
synchronous and session-free -- no `AsyncSession` parameter anywhere in
the package (structurally proven in the corpus test suite). A genuine
architecture win: of four consecutive Intelligence milestones, only K
ever needed new I/O beyond the graph queries J already made.

**One important consequence, found while building the real corpus**: a
test file where every changed line is a pure deletion (an assertion
removed with nothing added back) produces **zero** `ReviewCandidate`s
at all (`patchfrog.review.candidates._extract_added_lines` is the sole
input to candidate generation), so it never appears in any `ChangeUnit`
-- even though this is exactly the weakening this signal exists to
catch. `derive_weakened_test_expectations` never depends on the test
file having a `ReviewCandidate` of its own: it identifies *which* test
files to scan entirely from J's own `TEST_NOT_UPDATED` companions (see
"Test-only PRs stay quiet" above), and takes the `change_unit_id`/
`source_qualified_name` directly from that companion object -- the
actual line-count comparison then reads `diff_files` directly, so a
pure-deletion test file is still fully scanned even though it produced
no candidate of its own.

This same blind spot showed up one level higher, too: J's own
`status=OBSERVED`/`MISSING` split on a `TEST_NOT_UPDATED` companion is
itself derived from `all_changed_file_paths`, a set built from
generated candidates -- so J reports `MISSING` for a test file whose
only edit is a pure deletion, even though it really was touched.
`derive_weakened_test_expectations` therefore never filters on
`companion.status`: the companion establishes *only* the file-level
correlation to a changed production file; "was it genuinely touched"
is answered directly by real membership in `diff_files`, which is
strictly more accurate than J's own derived status.

## Review pipeline integration

Computed once per run, right after Intent Verification (consuming
Change Intelligence's already-built `ChangeUnit`s/
`ExpectedCompanionChange`s and the run's own `diff_files` directly --
no repository-graph query of its own, no database session at all). A
fourth optional `<test_intelligence>` prompt section
(`REVIEW_PROMPT_VERSION` 6 -> 7), attached only to the exact candidate
that is the source file of a real test gap -- empty for every other
candidate. `REVIEW_POLICY_VERSION`/`REVIEW_ENGINE_VERSION`/
`CHANGE_INTELLIGENCE_VERSION`/`CONTRACT_INTELLIGENCE_VERSION`/
`INTENT_VERIFICATION_VERSION` are **not** bumped -- nothing about
finding survival, orchestration, or those packages' own logic changed.
**No new agent role** -- Correctness and Security remain the only
specialists.

**Zero additional provider calls.** Structurally proven
(`test_test_intelligence_never_imports_a_session_type` and the absence
of any `LLMProvider` import anywhere in `patchfrog/test_intelligence/`).

## Change Story integration

`patchfrog.test_intelligence.story.build_test_story_prefix` produces at
most one bounded sentence ("Test impact: N changed symbol(s) with no
discoverable test surface; M touched test file(s) with a weakened
structural test signal."), prepended to the existing Change/Contract/
Intent Story text -- never a separate publication block, never a
separate persisted column. Empty unless at least one real gap exists.
Named "Test impact", not "Test coverage" -- see the next section.

## Conditional Test Impact summary

`patchfrog.test_intelligence.summary.should_render_test_gap_summary` is
a deterministic eligibility gate: shown whenever at least one real gap
exists (each gap is already selective evidence -- no additional
surface-count threshold is needed, unlike Intent Coverage's own
threshold, since a test gap is never redundant with what the Story
sentence already says). Format is a flat, bounded Markdown list
(`### Test impact` / `- symbol: reason`) -- **never a percentage,
never a confidence score, never a green/red badge**. Named "Test
impact" rather than "Test coverage" deliberately: PatchFrog does not
measure line/branch coverage, and a "coverage" heading would imply a
metric this milestone never computes. The internal Python/DB field
names (`test_coverage_summary_text`, `test_gap_candidate_count`, ...)
keep their original names -- only the user-facing rendered heading and
Story-prefix wording changed.

## Persistence

`review_runs` gained five nullable-default columns (migration
`0021_test_intelligence`): `test_expectation_count`,
`test_reason_code_counts`, `test_gap_candidate_count`,
`test_coverage_summary_rendered`, `test_coverage_summary_text`. **No
new text column for the Test Story prefix** -- it's folded into the
existing `change_story` column. `test_coverage_summary_text` IS a new,
dedicated text column (the Test Coverage block is its own separate
publication section, not a re-render of an existing one) -- needed
because publication runs as a separate, independently-retriable Celery
task from review generation, the same justification precedent J/K/L
already established. No test source code, no raw assertion text, and
no diff content are ever persisted by this package -- only bounded
counts and the already-rendered summary text.

## Telemetry and versioning

`patchfrog.telemetry.domain.TestIntelligenceTelemetry` (counts only --
no evidence text, no Test Story/Coverage prose) is a new field on
`ReviewTelemetrySnapshot`. Because `snapshot_to_dict` exports every
dataclass field via `dataclasses.asdict`, this is a real
exported-JSON-shape change, so `TELEMETRY_SCHEMA_VERSION` is bumped
4 -> 5 (applying the Milestone J correction / K/L precedent
proactively). Historical rows export `test_intelligence` with explicit
zero/default values.

`TEST_INTELLIGENCE_VERSION = 1` is introduced as this package's own
semantic-identity version. `CHANGE_INTELLIGENCE_VERSION`/
`CONTRACT_INTELLIGENCE_VERSION`/`INTENT_VERIFICATION_VERSION` are
**unchanged** -- none of those packages' own logic changed; only a
consuming package was added.

## Limitations

- Neither signal attributes a weakened assertion to a specific test
  *function* -- both are per-file. Per-function attribution would
  require parsing the diff against symbol boundaries, deferred rather
  than half-built.
- The marker regexes (`assert`/`pytest.raises`/`skip`/`xfail`) are
  Python/pytest-shaped; `is_test_path` itself is language-agnostic, but
  a non-Python test file is simply never flagged by
  `TEST_TOUCHED_BUT_WEAKENED` (fails closed, never guessed).
  `NO_TEST_SURFACE_FOUND` has no language dependency of its own -- it
  only depends on J's own `FILE_TESTS_FILE` edge detection.
- `NO_TEST_SURFACE_FOUND` is BEHAVIOR-kind-only (see "Scope
  restriction" above) -- a genuinely untested symbol inside a `MIXED`
  unit is not yet flagged.
- Says nothing about *why* a test wasn't written or was weakened (a
  deliberate decision, a time-pressured shortcut, or an oversight) --
  that judgment is left to the existing reviewer/critic, never
  fabricated here.
- Missing-surface/weakened-test candidates are heuristic evidence, not
  proof -- they must survive the existing reviewer/critic pipeline like
  any other finding before ever reaching GitHub.
