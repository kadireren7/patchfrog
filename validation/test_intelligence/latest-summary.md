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
re-flagged here -- see corpus case 6.

### What does L (Intent Verification) already detect, and is there overlap?

L's `PotentialIntentGap` fires only when a PR's *title/body* states an
explicit intent that maps (via bounded lexical overlap) to a real,
lexically-relevant `AffectedSymbolRef` that was not itself changed --
entirely conditioned on PR metadata text existing and being
sufficient. This milestone's signals never look at PR title/body at
all and fire (or don't) independent of whether any intent text exists.
The two candidate kinds can coexist for the same `ChangeUnit` without
duplicating each other (different evidence, different reason-code
namespace) -- see corpus case 14 for a coexistence proof. L already
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
- `TEST_TOUCHED_BUT_WEAKENED` needs only the *diff itself* --
  `DiffFile.added_lines`/`deleted_lines` (`patchfrog/diff/models.py`)
  are already fully parsed, in-memory, for every review run before
  Change Intelligence even runs. A structural (regex-only, no NLP,
  no LLM) count of assertion-like lines and skip/xfail-marker lines
  added vs. removed for any changed file that
  `patchfrog.indexing.inventory.is_test_path` (already-existing, pure,
  language-agnostic path heuristic -- the same function that produces
  `IndexedFileModel.is_test` at index time) identifies as a test file
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

### Self-caught bug during corpus authoring: pure-deletion test edits are invisible to `ChangeUnit`

The first implementation of `derive_weakened_test_expectations` iterated
`unit.changed_candidates` to find touched test files, mirroring J's own
`_test_staleness` structure. Building the real corpus case for "an
assertion was deleted with nothing added back" exposed that this is
wrong: `patchfrog.review.candidates._extract_added_lines` is the sole
input to candidate generation, so a diff hunk containing **only**
deletions produces zero `ReviewCandidate`s for that file -- it therefore
never appears in any `ChangeUnit` at all, even though a bare assertion
deletion is exactly the kind of silent test erosion this signal exists
to catch (arguably the highest-value case, since the file may then
receive *no* AI review attention whatsoever). Fixed by scanning
`diff_files` directly for `is_test_path` matches, entirely decoupled
from `ChangeUnit.changed_candidates` -- a real `ChangeUnit` id is still
attributed when one happens to touch the same file (the common case:
most weakening edits touch at least one other line too), falling back
to a deterministic synthetic id (`f"standalone:{file_path}"`) when none
does. Two corpus cases (the assertion-removal case and a title/token
mismatch in an unrelated coexistence case, see below) initially passed
"for the wrong reason" -- a pure-deletion fixture that never exercised
the real counting logic at all because no candidate existed -- until
this was caught; both were re-verified to genuinely exercise the fixed
logic afterward. Mirrors Milestone L's own "check whether the fixture
actually produces the diff you think it does" lesson.

A second, smaller self-caught issue in the same pass: a coexistence
corpus case's PR title used "retries" (plural) while the affected
callee's own name tokenized to "retry" (singular) -- L's deterministic
lexical matcher never stems, so the claim and the affected-surface node
shared no token and the expected `PotentialIntentGap` never appeared.
Fixed by aligning the title's wording with the real identifier's token
exactly (matching Milestone L's own corpus's phrasing convention) --
not a code bug, a fixture-wording bug caught by the same "does the
fixture actually produce what I think it does" discipline.

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
`change_story`, a new conditional `### Test coverage` publication
block (its own persisted `test_coverage_summary_text` column, mirroring
Intent Coverage), five new nullable-default `review_runs` columns
(migration `0021_test_intelligence`), a new `TestIntelligenceTelemetry`
counts-only field on `ReviewTelemetrySnapshot` (`TELEMETRY_SCHEMA_VERSION`
4 -> 5). No new agent role, no new provider call, no new repository-graph
query, no new base-commit fetch.

## 4. Corpus results (18/18 real-stack scenarios, spec section 31 minimum)

All cases use a real git fixture repository, real indexing
(`RepositoryIndexingService`), a real diff (`diff_against_base`), real
`ReviewCandidateGenerator`/`build_change_intelligence_report` output,
and (where relevant) real `build_contract_intelligence_report`/
`build_intent_verification_report` output. Zero FakeLLM-authored ground
truth anywhere.

| # | Case | Result |
|---|------|--------|
| 1 | New, entirely untested BEHAVIOR function | `NO_TEST_SURFACE_FOUND` gap |
| 2 | Existing test file found but MISSING (J's own territory) | dedup: 0 gaps |
| 3 | Existing test touched normally, no weakening | 0 gaps |
| 4 | Real assertion removed from a touched test | `TEST_TOUCHED_BUT_WEAKENED` gap |
| 5 | Assertions strengthened | 0 gaps |
| 6 | Only an unused import removed (neutral) | 0 gaps |
| 7 | `@pytest.mark.skip` newly added | `TEST_TOUCHED_BUT_WEAKENED` gap |
| 8 | `@pytest.mark.skip` removed (un-skip) | 0 gaps (strengthening) |
| 9 | Real cross-file caller -> CONTRACT-kind, untested | 0 `NO_TEST_SURFACE_FOUND` gaps (K's territory) |
| 10 | CONFIGURATION-kind file change | 0 gaps |
| 11 | MIXED unit (behavior + infra in one component) | 0 `NO_TEST_SURFACE_FOUND` gaps |
| 12 | Real K stale consumer + a separate untested BEHAVIOR change | both fire, independently |
| 13 | Real L intent gap + a separate test gap | both fire, independently |
| 14 | INFRASTRUCTURE-kind file change | 0 gaps |
| 15 | New behavior + a brand-new test file added in the same PR | 0 gaps (J's OBSERVED companion suppresses) |
| 16 | Real `review_local` pipeline run | persisted correctly on `ReviewRunModel` |
| 17 | Telemetry/versioning round trip on a real report | version + counts correct |
| 18 | Structural: no `AsyncSession` import anywhere in the package | proven via AST scan |

## 5. Self-caught issues during corpus authoring

See section 1's "Self-caught bug during corpus authoring" above for
the full narrative: (1) a real architecture fix -- pure-deletion test
edits produce zero `ReviewCandidate`s, so `TEST_TOUCHED_BUT_WEAKENED`
detection was redesigned to scan `diff_files` directly rather than
`ChangeUnit.changed_candidates`; (2) a fixture-wording fix -- a
coexistence case's PR title used "retries" where the real identifier
tokenized to "retry" (no stemming, by design), breaking L's lexical
match. Both caught by re-verifying that each fixture's real git diff
produced the exact evidence the test claimed, before trusting a
passing assertion.

## 6. Gates (final)

- `ruff check .`: clean (488 files scanned, whole repo).
- `mypy . --strict`: clean, 488 source files.
- `pytest tests/`: **1576/1576 passing** (1178 unit + 398 integration),
  against real Postgres/Redis.
- Alembic: single head (`0021_test_intelligence`), real upgrade applied
  cleanly on top of `0020_intent_verification`.
- Both Docker images (`api`, `worker`) built clean from `docker/Dockerfile`.
- Secret scan (regex over the full staged diff): clean.
- `git diff --check`: clean, no whitespace errors.
- No Co-Authored-By trailer, no secret material, in the diff.

## 7. Versioning (final)

- `TEST_INTELLIGENCE_VERSION = 1` (new).
- `REVIEW_PROMPT_VERSION`: 6 -> 7 (new `<test_intelligence>` section).
- `TELEMETRY_SCHEMA_VERSION`: 4 -> 5 (new `test_intelligence` telemetry field).
- `REVIEW_POLICY_VERSION`/`REVIEW_ENGINE_VERSION`/`CONFIG_SCHEMA_VERSION`/
  `QUALITY_COST_POLICY_VERSION`/`CHANGE_INTELLIGENCE_VERSION`/
  `CONTRACT_INTELLIGENCE_VERSION`/`INTENT_VERIFICATION_VERSION`: all
  unchanged, each pinned by `tests/unit/test_test_intelligence_versioning.py`.
