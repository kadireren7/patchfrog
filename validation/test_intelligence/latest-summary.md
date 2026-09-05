# Test Intelligence Foundation — Audit & Validation

Milestone M. Deterministic, evidence-based detection of when changed
*behavior* lacks the correct behavioral test surface -- never a
coverage-percentage product, never a test-generation agent, never a
mutation-testing system, never an LLM-only classifier (spec's explicit
non-goals). Extends [[patchfrog_change_intelligence_foundation]] (J),
[[patchfrog_contract_blast_radius_intelligence]] (K), and
[[patchfrog_intent_verification_foundation]] (L) rather than building a
fourth parallel graph/candidate stack.

## 1. Audit (written before any implementation)

### What does J already detect about tests, and what does it NOT detect?

`patchfrog.change_intelligence.companions._test_staleness` (see
`patchfrog/change_intelligence/companions.py`) already produces an
`ExpectedCompanionChange` with `reason_code=CompanionReasonCode.TEST_NOT_UPDATED`
for every changed file that has at least one real `FILE_TESTS_FILE`
graph edge to a likely test file -- `status=OBSERVED` when that test
file was itself touched in the diff, `status=MISSING` when it was not.

This is **only ever constructed when a likely test file already
exists** in the repository graph. J is silent -- produces literally
nothing -- when `likely_tests_for_file` returns zero edges for a
changed file. That silence is exactly the gap this milestone exists to
fill: "this changed behavior has no discoverable test file at all" is
a categorically different, and arguably more important, signal than
"a known test file wasn't updated." J's own module docstring/spec never
claimed to cover this case -- it is a real, non-overlapping gap, not a
duplicate of J's own candidate space.

Symmetrically, J is also silent about *how* a touched test file
changed -- `_test_staleness` only ever looks at whether the test
file's path appears in the diff's changed-file set, never at what
changed inside it. A test file can be "touched" in the sense J checks
while having its actual behavioral assertions weakened or removed
entirely (an assertion deleted, a `pytest.mark.skip` added) -- J would
correctly report `OBSERVED` (no actionable candidate) for exactly the
case that most needs a candidate. This is the second genuinely new
signal.

**Reuse/dedup rule, stated precisely** (mirrors L's own "never build a
second near-duplicate candidate" discipline): a changed file is only
ever eligible for this milestone's `NO_TEST_SURFACE_FOUND` signal when
**no** `ExpectedCompanionChange` with `reason_code=TEST_NOT_UPDATED`
and `source_file_path` equal to that file exists among the companions
J (and, when reused for K's stale-consumer objects passed through the
same list) already produced -- checked regardless of that companion's
own `status` (even a `MISSING` `TEST_NOT_UPDATED` companion means "a
test file *was* found," which is J's territory, not this milestone's).

**Test-only PRs stay quiet -- Test Intelligence is not an inverse
feature detector.** `TEST_TOUCHED_BUT_WEAKENED` (see below) is
deliberately **anchored** to a real, same-PR *production* change: a
test file is only ever eligible for it when a real
`ExpectedCompanionChange` with `reason_code=TEST_NOT_UPDATED` already
names it as `expected_file_path` -- i.e. J's own companion machinery
already confirms this test file is linked to a changed production
file. This is sound, not a heuristic guess, because
`_test_staleness` iterates the *production* side only: for each
changed production candidate, it looks up that candidate's own linked
test files; it never runs in the reverse direction (starting from a
changed test file and asking what it tests). A PR that touches only
test files therefore produces **zero** `TEST_NOT_UPDATED` companions
of any status for any file -- so this signal structurally cannot fire
without a real production change in the same PR. Proven directly by
the corpus's two mandatory negative cases (pure test-only assertion
removal, pure test-only skip/xfail addition) and a precision case (an
unrelated pre-existing test weakened while a *different* production
file changes elsewhere in the same PR never fires, since the
correlation is per-file, never "any production change in the PR
unlocks any touched test").

### What does K (Contract & Blast Radius Intelligence) already detect, and is there overlap?

K's `stale_consumers` are `ExpectedCompanionChange` objects with
`reason_code=CONTRACT_CONSUMER_NOT_UPDATED` -- about a *caller* of a
breaking-signature function not being updated, never about test
surface. Zero overlap in candidate shape. The one place K interacts
with this milestone's scope is indirect: a function with a real
cross-file caller is classified `ChangeKind.CONTRACT` by
`patchfrog.change_intelligence.change_kind.classify_candidate`, not
`BEHAVIOR` -- and (see "Scope restriction" below) this milestone's
`NO_TEST_SURFACE_FOUND` signal is deliberately restricted to
`ChangeUnit`s whose combined `change_kind` is exactly `BEHAVIOR`. A
`CONTRACT`-kind change with a real caller is `K`'s own territory (a
missing/stale *consumer*, not a missing *test*) and is correctly never
re-flagged here -- see corpus case 9.

### What does L (Intent Verification) already detect, and is there overlap?

L's `PotentialIntentGap` fires only when a PR's *title/body* states an
explicit intent that maps (via bounded lexical overlap) to a real,
lexically-relevant `AffectedSymbolRef` that was not itself changed --
entirely conditioned on PR metadata text existing and being
sufficient. This milestone's signals never look at PR title/body at
all and fire (or don't) independent of whether any intent text exists.
The two candidate kinds can coexist for the same `ChangeUnit` without
duplicating each other (different evidence, different reason-code
namespace) -- see corpus cases 13/16 for coexistence proofs (with a gap, and fully resolved). L already
established the precedent of referencing another package's objects by
instance rather than re-deriving them (`IntentCoverage.relevant_companion_candidates`);
this milestone follows the same discipline for J's `TEST_NOT_UPDATED`
objects (see above), never copying data out of them.

### Architecture decision: does this need a new database query?

No. Both new signals are fully computable from data every review run
already builds before this package would run:

- `NO_TEST_SURFACE_FOUND` needs only `ChangeUnit.change_kind`,
  `ChangeUnit.changed_candidates` (`file_path`/`qualified_name`/
  `symbol_id`), and the combined `expected_companions` list (J's own
  `TEST_NOT_UPDATED` objects, already computed once per run). No new
  `RepositoryQueryService` call.
- `TEST_TOUCHED_BUT_WEAKENED` needs the combined `expected_companions`
  list (to identify *which* test files correlate to a real production
  change -- see "Test-only PRs stay quiet" above) and the PR's own
  already-parsed `DiffFile.added_lines`/`deleted_lines`
  (`patchfrog/diff/models.py`, already built for every review run
  before Change Intelligence even runs) for the actual structural
  comparison. A regex-only (never NLP, never LLM) count of
  assertion-like lines and skip/xfail-marker lines added vs. removed
  answers the question with zero additional I/O of any kind -- not
  even K's own one bounded base-content git fetch was needed.

**This means `patchfrog.test_intelligence` can be, like L, entirely
synchronous and session-free** -- no `AsyncSession` parameter anywhere
in the package, a strictly stronger zero-I/O position than K (which
needed one new bounded base-commit fetch) and equal to L. This is a
genuine, documented architecture win worth calling out: three
consecutive Intelligence milestones now, and only K ever needed new
I/O beyond the graph queries J already made.

### Why no `lexical.py` / `mapping.py` for this milestone

L needed bounded lexical-overlap matching because PR *prose* had to be
mapped to graph objects with no shared identifier. Neither of this
milestone's signals ever compares prose to anything -- `NO_TEST_SURFACE_FOUND`
is a pure existence check over already-attributed objects (same
`file_path`), and `TEST_TOUCHED_BUT_WEAKENED` is a pure line-count
comparison within one file's own diff. There is no claim-to-surface
mapping problem here to solve, so introducing a lexical module would
be unused machinery, not future-proofing (matching this codebase's own
anti-abstraction discipline).

### Scope restriction: why `NO_TEST_SURFACE_FOUND` is BEHAVIOR-kind-only

`ChangeUnit.change_kind` is `combine_kinds` over every constituent
candidate's own `classify_candidate` result -- it is exactly one
non-`MIXED` value only when *every* candidate in the unit agrees. A
`BEHAVIOR`-kind unit is therefore guaranteed (by construction) to
contain no test file, no config/infra/persistence-path file, and no
symbol with a real cross-file caller (which would classify `CONTRACT`
instead). This is the narrowest, most honest scope for "a genuinely
new/changed piece of business logic with nothing else going on" --
deliberately excluding `MIXED` units (which may contain an untested
`BEHAVIOR` candidate alongside, say, a `CONFIGURATION` candidate) for
this first version. This is a documented, conservative limitation, not
an oversight -- widening it to per-candidate (rather than per-unit)
classification inside `MIXED` units is a natural, safe follow-up but
is deferred rather than risking a broader, less-audited first cut
(mirrors J/K/L's own repeated "prefer the smaller, provably-correct
surface first" discipline).

### Which structural markers count as "weakened," and why regex-only

Never NLP, never an LLM judgment about whether a test is "good."
Exactly two structural signals, both counted from raw diff line text
(`DiffLine.content`), each independently sufficient to flag
`TEST_TOUCHED_BUT_WEAKENED`:

1. **Net assertion-marker count decreased**: lines matching
   `assert `, `self.assert*(`, `pytest.raises(`, `with raises(` are
   counted in `deleted_lines` vs. `added_lines`; a strictly negative
   net (more removed than added) flags the file. A file where
   assertions were only *added*, or where the count is unchanged
   (e.g. only imports/mocks/comments changed), never flags.
2. **A skip/xfail marker was newly added**: `@pytest.mark.skip`,
   `@pytest.mark.xfail`, `pytest.skip(`, `pytest.importorskip(` counted
   the same way; a strictly positive net (more added than removed)
   flags the file. *Removing* a skip marker (un-skipping a test) has a
   negative net and correctly never flags -- that is strengthening,
   not weakening.

Both are per-file, not per-test-function -- attributing a assert-count
delta to one specific test function inside a file would require
parsing the diff against symbol boundaries, which is real future work
this milestone deliberately does not attempt (see docs for the
explicit deferral, mirroring K's own `ContractKind` deferrals).

### Self-caught/externally-flagged issues during development (final, post-correction)

**Issue 1 (self-caught, initial build): pure-deletion test edits are
invisible to `ChangeUnit`.** The first implementation of
`derive_weakened_test_expectations` iterated `unit.changed_candidates`
to find touched test files, mirroring J's own `_test_staleness`
structure. Building the real corpus case for "an assertion was deleted
with nothing added back" exposed that this is wrong:
`patchfrog.review.candidates._extract_added_lines` is the sole input to
candidate generation, so a diff hunk containing **only** deletions
produces zero `ReviewCandidate`s for that file -- it therefore never
appears in any `ChangeUnit` at all, even though a bare assertion
deletion is exactly the kind of silent test erosion this signal exists
to catch. First fix: scan `diff_files` directly rather than
`ChangeUnit.changed_candidates` to *identify* which files to consider.

**Issue 2 (externally flagged, correction round): the first fix let a
test-only PR produce a gap.** Scanning *every* touched test file in
`diff_files` (as Issue 1's fix did) meant `TEST_TOUCHED_BUT_WEAKENED`
could fire for a PR that touches nothing but tests -- directly
violating the spec's "not an inverse feature detector" requirement and
its mandatory test-only negative case. Fixed by **anchoring** the
signal to a real `ExpectedCompanionChange` with
`reason_code=TEST_NOT_UPDATED` naming that exact test file as
`expected_file_path` -- see "Test-only PRs stay quiet" above for why
this is sound (companions are only ever derived from the *production*
side, never the reverse). The `f"standalone:{file_path}"` synthetic
change-unit-id fallback from Issue 1's fix was removed entirely: the
companion object itself already carries a real `change_unit_id`
(traced back to the production candidate that produced it), which is
both simpler and more correct than any fallback.

**Issue 3 (self-caught while re-verifying Issue 2's fix): anchoring on
`companion.status is OBSERVED` reintroduced Issue 1's own blind spot.**
The natural first anchor -- require `status=OBSERVED` on the
`TEST_NOT_UPDATED` companion -- turned out to be unsound: that status
is itself derived from `all_changed_file_paths`, a set built from
generated `ReviewCandidate`s, so it inherits the exact added-lines-only
blind spot Issue 1 fixed at the file-identification layer. A real
corpus run of "production changes, and its already-existing test loses
an assertion via pure deletion" produced `status=MISSING` for the
correlating companion (since the test file's pure-deletion edit
produced no candidate) -- so gating on `OBSERVED` would have silently
dropped exactly the case Issue 1 was written to catch. Fixed by
dropping the `status` filter entirely: the companion is used *only* to
establish the file-level correlation to a changed production file;
whether the test file was genuinely touched is answered independently,
and more precisely, by direct membership in `diff_files` (real ground
truth, not a derived approximation). Both the test-only negative
requirement and the pure-deletion positive case now hold simultaneously
-- proven together in the corpus (`test_case_weakened_assertions_removed`
is exactly this pure-deletion-plus-production-change scenario, and
correctly produces a `MISSING`-status companion alongside a real gap).

**Issue 4 (self-caught): the module docstring's original "18/18"
framing conflated behavioral scenarios with structural/pipeline
tests.** External review correctly noted that a telemetry-serialization
test, a version-pin test, and a structural zero-`AsyncSession`-import
proof are not themselves behavioral evaluation scenarios. Section 4/8
below now report the two counts separately.

**Issue 5 (self-caught): a coexistence corpus case's PR title used
"retries" (plural) while the affected callee's own name tokenized to
"retry" (singular)** -- L's deterministic lexical matcher never stems,
so the claim and the affected-surface node shared no token and the
expected `PotentialIntentGap` never appeared. Fixed by aligning the
title's wording with the real identifier's token exactly (matching
Milestone L's own corpus's phrasing convention) -- not a code bug, a
fixture-wording bug caught by the same "does the fixture actually
produce what I think it does" discipline every prior milestone's own
corpus work has relied on.

### What is explicitly out of scope / deferred (never faked)

- Per-test-function attribution of a weakened assertion (see above).
- Any signal for non-Python test frameworks' framework-specific
  weakening idioms beyond the generic `assert`/`pytest.raises`/
  `skip`/`xfail` markers already covered -- `is_test_path` itself is
  language-agnostic, but the marker regexes here are Python/pytest-
  shaped; a non-Python test file is simply never flagged by signal 2
  (fails closed, never guessed).
- Widening `NO_TEST_SURFACE_FOUND` to `MIXED` units (see above).
- A numeric "coverage score" of any kind -- never built, per spec.
- Actually generating a test, or suggesting exact test code -- never
  built, per spec; the reviewer prompt only ever receives evidence
  text, exactly like J/K/L.

## 2. Domain model and architecture

See `patchfrog/test_intelligence/domain.py`. Four types, matching the
spec's own naming, all genuinely constructed and consumed (none is a
documented-but-unused placeholder):

- `TestSurface` -- the discovered test-file linkage for one changed
  file, derived purely by cross-referencing that file against J's own
  `TEST_NOT_UPDATED` companions (`expectations.derive_test_surfaces`);
  `known_test_file_paths` empty means genuinely none found.
- `TestEvidence` -- the bounded, already-rendered structural evidence
  behind one expectation (an exact assertion/skip-marker count
  comparison).
- `TestExpectation` -- one candidate, mirroring
  `ExpectedCompanionChange`'s own role exactly (reuses
  `CompanionStatus` for OBSERVED/MISSING); this milestone only ever
  constructs `MISSING` expectations.
- `PotentialTestGap` -- the actual candidate surfaced to review,
  constructed 1:1 from a `MISSING` `TestExpectation` (references it by
  instance, never re-derives its fields).

All four dataclasses/the enum carry a `__test__ = False` (or
`ClassVar[bool]` for the frozen dataclasses) class attribute -- pytest's
default collection otherwise emits a spurious "cannot collect ... has
an `__init__` constructor" warning for any test module that imports a
class named `Test*`, which every one of these legitimately is.

`TEST_INTELLIGENCE_VERSION = 1` (new, independent of
`CHANGE_INTELLIGENCE_VERSION`/`CONTRACT_INTELLIGENCE_VERSION`/
`INTENT_VERIFICATION_VERSION`, all three of which this milestone
leaves untouched).

## 3. Review pipeline integration

Wired at the exact same point J/K/L already established
(`PullRequestReviewService._execute_and_persist`, right after Intent
Verification): a fourth optional `<test_intelligence>` prompt section
(`REVIEW_PROMPT_VERSION` 6 -> 7), a Test Story prefix folded into
`change_story`, a new conditional `### Test impact` publication block
(its own persisted `test_coverage_summary_text` column, mirroring
Intent Coverage), five new nullable-default `review_runs` columns
(migration `0021_test_intelligence`), a new `TestIntelligenceTelemetry`
counts-only field on `ReviewTelemetrySnapshot` (`TELEMETRY_SCHEMA_VERSION`
4 -> 5). No new agent role, no new provider call, no new repository-graph
query, no new base-commit fetch.

**User-facing wording**: the rendered heading is `### Test impact`, not
`### Test coverage` -- PatchFrog does not measure line/branch coverage,
and a "coverage" heading would misleadingly imply it does. Same for the
Change Story prefix ("Test impact: ..."). Only the *rendered* text
changed; internal Python/DB field and column names
(`test_coverage_summary_text`, `test_gap_candidate_count`, ...) keep
their original names, since renaming internal identifiers would be
unrelated churn.

## 4. Corpus results (21 behavioral scenarios + 3 supporting tests)

All cases use a real git fixture repository, real indexing
(`RepositoryIndexingService`), a real diff (`diff_against_base`), real
`ReviewCandidateGenerator`/`build_change_intelligence_report` output,
and (where relevant) real `build_contract_intelligence_report`/
`build_intent_verification_report` output. Zero FakeLLM-authored ground
truth anywhere. **21 behavioral corpus scenarios**, exceeding the
spec's 18-scenario minimum, plus **3 supporting integration/structural
tests** deliberately not counted toward that total (see section 8's
explicit accounting and matrix against the spec's own 18 named
scenarios).

| # | Case | Result |
|---|------|--------|
| 1 | New, entirely untested BEHAVIOR function | `NO_TEST_SURFACE_FOUND` gap |
| 2 | Existing test file found but MISSING (J's own territory) | dedup: 0 gaps |
| 3 | Existing test touched normally, no weakening | 0 gaps |
| 4 | Production changed + its linked test's assertion removed | `TEST_TOUCHED_BUT_WEAKENED` gap |
| 4a | **Mandatory negative**: same test edit, but test-only (no production change) | 0 gaps -- structurally cannot fire |
| 5 | Assertions strengthened (production+test both present) | 0 gaps |
| 6 | Only an unused import removed (neutral, production+test both present) | 0 gaps |
| 7 | Production changed + `@pytest.mark.skip` newly added to its linked test | `TEST_TOUCHED_BUT_WEAKENED` gap |
| 7a | **Mandatory negative**: same skip addition, but test-only | 0 gaps |
| 8 | Production changed + `@pytest.mark.skip` removed (un-skip) | 0 gaps (strengthening) |
| 8a | **Precision check**: unrelated pre-existing test weakened while a *different* production file changes elsewhere | 0 gaps for the unrelated test (per-file anchor, not "any production change unlocks any test") |
| 9 | Real cross-file caller -> CONTRACT-kind, untested | 0 `NO_TEST_SURFACE_FOUND` gaps (K's territory) |
| 10 | CONFIGURATION-kind file change | 0 gaps |
| 11 | MIXED unit (behavior + infra in one component) | 0 `NO_TEST_SURFACE_FOUND` gaps |
| 12 | Real K stale consumer + a separate untested BEHAVIOR change | both fire, independently |
| 13 | Real L intent gap + a separate test gap (test unchanged) | both fire, independently |
| 14 | INFRASTRUCTURE-kind file change | 0 gaps |
| 15 | New behavior + a brand-new test file added in the same PR | 0 gaps (J's OBSERVED companion suppresses) |
| 16 | Intent-mapped behavior + relevant test **updated** (companion to 13) | both L and M report clean/no gap |
| 17 | Docs-only change | 0 gaps, no crash |
| 18 | Two unrelated ChangeUnits, each independently untested | two distinct gaps, never merged/conflated |
| 19 | Large fan-out: one function, 7 independently-linked test files, all weakened | bounded to `MAX_TEST_GAPS_PER_UNIT` (5), not one per file |
| 20 | A related test file is **deleted** in the same commit | `NO_TEST_SURFACE_FOUND` fires (J's post-deletion graph has no edge -- equivalent to "no test ever found") |
| 21 | **Stale-gap regression**: head A has a real gap; head B (same branch) adds the test and is recomputed from scratch against the new exact head | the head-A gap does not survive into the head-B report |

**Supporting integration/structural tests** (not counted above): a
real `review_local` pipeline run persisting Test Intelligence onto
`ReviewRunModel`; a telemetry/versioning round trip on a real
corpus-built report; a structural AST proof that no module in
`patchfrog/test_intelligence/` imports `AsyncSession`.

## 5. Self-caught/externally-flagged issues

See section 1's "Self-caught/externally-flagged issues" above for the
full five-issue narrative (pure-deletion invisibility -> anchor
introduced -> anchor's own status-filter blind spot found and removed
-> accounting corrected -> a lexical-matching fixture-wording fix).
Each was caught by re-verifying that a fixture's real git diff/real
companion output produced the exact evidence the test claimed, before
trusting a passing assertion -- the same discipline every prior
milestone's own corpus work has relied on.

## 6. Gates (final, post-correction)

- `ruff check .`: clean, whole repo.
- `mypy . --strict`: clean, whole repo.
- `pytest tests/`: full suite passing against real Postgres/Redis (see
  the final report for the exact post-correction total).
- Alembic: single head (`0021_test_intelligence`), real upgrade applied
  cleanly on top of `0020_intent_verification`.
- Both Docker images (`api`, `worker`) built clean from `docker/Dockerfile`.
- Secret scan (regex over the full staged diff): clean.
- `git diff --check`: clean, no whitespace errors.
- No Co-Authored-By trailer, no secret material, in the diff.

## 7. Versioning (final)

- `TEST_INTELLIGENCE_VERSION = 1` -- this is the *corrected*, final
  semantics (test-only-quiet, status-independent diff-anchored
  correlation): the version was never bumped mid-correction because
  PR #45 had not yet merged, so there was no prior "v1" behavior a
  consumer could have already observed and relied on. `1` names this
  final design, not the pre-correction one.
- `REVIEW_PROMPT_VERSION`: 6 -> 7 (new `<test_intelligence>` section).
- `TELEMETRY_SCHEMA_VERSION`: 4 -> 5 (new `test_intelligence` telemetry field).
- `REVIEW_POLICY_VERSION`/`REVIEW_ENGINE_VERSION`/`CONFIG_SCHEMA_VERSION`/
  `QUALITY_COST_POLICY_VERSION`/`CHANGE_INTELLIGENCE_VERSION`/
  `CONTRACT_INTELLIGENCE_VERSION`/`INTENT_VERIFICATION_VERSION`: all
  unchanged, each pinned by `tests/unit/test_test_intelligence_versioning.py`.

## 8. Spec section 31 scenario matrix (18 named scenarios, explicit accounting)

| # | Spec scenario | Status | Corpus case(s) |
|---|---------------|--------|-----------------|
| 1 | Behavior changed + related test updated | SUPPORTED | 3 |
| 2 | Behavior changed + related test unchanged (J dedup) | SUPPORTED | 2 |
| 3 | No known related test | SUPPORTED | 1 |
| 4 | Contract change + related test unchanged | SUPPORTED (scope: K's territory, M correctly quiet) | 9 |
| 5 | Intent-mapped behavior + relevant test unchanged | SUPPORTED | 13 |
| 6 | Intent-mapped behavior + relevant test updated | SUPPORTED | 16 |
| 7 | Negative/error-path test missing | **DEFERRED** -- see below | -- |
| 8 | Complete implementation + tests | SUPPORTED | 3, 15 |
| 9 | Docs-only | SUPPORTED | 17 |
| 10 | Test-only | SUPPORTED (mandatory negatives) | 4a, 7a |
| 11 | Internal helper (no external consumer) | SUPPORTED -- M has no K-style "externally consumed" restriction; an untested private helper is flagged the same as any other untested BEHAVIOR symbol | 1 |
| 12 | Unrelated test changed | SUPPORTED | 8a |
| 13 | Stale gap disappears on later head | SUPPORTED | 21 |
| 14 | J TEST_NOT_UPDATED dedup | SUPPORTED | 2 |
| 15 | K/L coexistence/dedup | SUPPORTED | 12, 13 |
| 16 | Two unrelated ChangeUnits / separate tests | SUPPORTED | 18 |
| 17 | Large fan-out boundedness | SUPPORTED | 19 |
| 18 | Deleted related test | SUPPORTED | 20 |

**Scenario 7, DEFERRED, with technical reason**: "does the test surface
exercise a *specific* code path (e.g. an error/exception branch)"
requires reasoning about branch/path coverage -- fundamentally a
different granularity than this milestone's file-existence and
gross-assertion-count signals. Answering it correctly would require
either (a) real line/branch coverage instrumentation (which PatchFrog
explicitly does not run, and which this milestone's own non-goals
rule out), or (b) parsing the diff against control-flow/AST boundaries
to determine which branch of the changed function a given assertion
exercises -- real, substantial future work, not a small extension of
the existing regex-based markers. Never faked; not claimed as passing
anywhere in this document, the code, or the PR description.
