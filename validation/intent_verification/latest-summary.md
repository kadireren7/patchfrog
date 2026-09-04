# Intent Verification Foundation — Audit & Validation

Branch `feat/intent-verification`, baseline `main` @
`9be10933d04474f6cfda05f7451d82101e8bdd4c` (Milestone K, merged).

## 1. Audit (written before any implementation)

### Which intent sources already exist in persisted/runtime data?

- **PR title**: available twice over. `patchfrog.domain.pull_request.PullRequestMetadata.title`
  is fetched fresh, live, on every real review
  (`apps/worker/tasks/review_pull_request.py`'s `current_metadata =
  await github_client.get_pull_request(...)`, the exact same fetch that
  already supplied `base_sha` for Milestone K). It is also durably
  persisted on `patchfrog.persistence.models.pull_request.PullRequestModel.title`
  (`String(1024)`) at ingestion time (`patchfrog.github.webhooks`
  extracts `pull_request_title` from the raw webhook payload).
- **PR body**: available live, fetched fresh on every real review
  (`PullRequestMetadata.body: str | None`) -- but **never persisted**.
  `PullRequestModel` has no `body` column at all (confirmed by reading
  `patchfrog/persistence/models/pull_request.py`). This mirrors exactly
  the Milestone K finding about `base_sha`: already-fetched, live, free
  data that simply isn't threaded to where Change/Contract Intelligence
  run. No new GitHub call is needed to use it during review -- only
  during review, since nothing durable stores it for later.
- **Linked issue references**: **do not exist anywhere in this codebase**
  (confirmed: `grep -rn "linked_issue\|closes #\|fixes #\|resolves
  #\|get_issue\b"` across `patchfrog/`/`apps/` returns zero hits). There
  is no issue-body fetch, no reference-parsing, nothing to reuse.
- **Commit messages**: `patchfrog.github.client.GitHubClient` has no
  "list PR commits" method at all (confirmed: only `get_pull_request`,
  `get_default_branch_head_sha`, `list_pull_request_files`,
  `list_pull_request_reviews`, `create_pull_request_review`,
  `list_pull_request_review_comments`, `list_review_comment_reactions`,
  `list_review_thread_statuses` exist). Fetching commit messages would
  be a genuinely new GitHub API call, not reused data.
- **Changed tests**: fully available today, zero new plumbing --
  `patchfrog.persistence.models.repository_index.IndexedFileModel.is_test`
  is already computed at index time, and
  `RepositoryQueryService.likely_tests_for_file` (already used by
  Change Intelligence's `TEST_NOT_UPDATED` companion heuristic) already
  answers "is this a graph-linked test for a changed file."

### Which sources are reliable enough for Milestone L? Which are deferred?

**Implemented this milestone, as real `IntentEvidence`**: `PR_TITLE`,
`PR_BODY` -- both EXPLICIT, zero new GitHub calls, already-fetched live
metadata.

**`TEST_CHANGE` -- audited and corrected mid-milestone (see the
correction round below): NOT emitted as `IntentEvidence`.** The
originally-drafted claim that extraction "produces `TEST_CHANGE`
evidence" was inaccurate -- `extract_claims_from_pr_metadata` only ever
constructs `PR_TITLE`/`PR_BODY` evidence, and the service only ever
receives title/body plus already-computed J/K evidence. The real
"changed tests strengthen coverage" signal this milestone provides is a
different, simpler mechanism, not a `TEST_CHANGE`-kind `IntentEvidence`
object: when an already-existing `TEST_NOT_UPDATED`
`ExpectedCompanionChange` (Change Intelligence's own test-relationship
evidence) belongs to a claim's mapped `ChangeUnit`, it's referenced via
`IntentCoverage.relevant_companion_candidates` -- the same dedup
mechanism used for every other J/K companion. `IntentSourceKind.TEST_CHANGE`
remains defined on the enum, explicitly documented as reserved for a
future, more direct per-test signal, never emitted this milestone.

**Deferred, explicitly, per spec sections 18/19's own permission**:

- `LINKED_ISSUE` -- would require entirely new GitHub API plumbing (an
  issue-body fetch by number, new error handling for a private/deleted/
  cross-repo issue, a new installation-token permission surface to
  reason about). This is "expand scope substantially," not free reuse
  -- deferred per spec section 18's explicit instruction.
- `COMMIT_MESSAGE` -- would require a new `list_pull_request_commits`
  GitHub API method that does not exist today. Deferred per spec
  section 19's explicit instruction ("If commit messages are not
  already cheaply available: defer them").

`IntentSourceKind` keeps all five values (matching spec section 2's
suggested taxonomy) for forward documentation, but this milestone's
extraction logic only ever emits `PR_TITLE`/`PR_BODY` `IntentEvidence`.

### How Intent Verification reuses J/K rather than creating a parallel architecture

Nothing here re-derives what changed or what it affects:

- **ChangeUnit mapping** operates purely on already-built
  `patchfrog.change_intelligence.domain.ChangeUnit` objects (title,
  changed candidates, already-computed `affected_surface`) -- no new
  grouping, no new traversal.
- **Expected/relevant surface** comes entirely from J's
  `AffectedSymbolRef` (`derive_affected_surface`, already computed) and
  K's `ContractDelta`/blast radius -- Intent Verification never invents
  an affected surface from prose (spec section 10's hard requirement).
  It only *filters* the surface J/K already computed by lexical
  relevance to an explicit intent claim.
- **Dedup against J/K** (spec section 14): rather than construct a
  second, near-duplicate warning for a missing surface J/K already flag
  (a `CALLER_NOT_UPDATED`/`TEST_NOT_UPDATED`/`CONTRACT_CONSUMER_NOT_UPDATED`
  `ExpectedCompanionChange` already `MISSING`), Intent Verification
  *tags the existing object as intent-relevant*
  (`IntentCoverage.relevant_companion_candidates`, a tuple of
  references to the same, already-existing `ExpectedCompanionChange`
  instances) -- it never constructs a second `PotentialIntentGap` for
  the same underlying missing surface. `PotentialIntentGap` (a new
  type) is reserved for the one genuinely new signal this milestone
  adds: a real `AffectedSymbolRef` that J/K never flagged as "missing"
  (because J/K have no concept of relevance to explicit intent) but
  which is lexically relevant to an explicit claim and was not itself
  changed. See section 2 below for the exact reason-code split.
- **No second diagram, no second Change Map**: intent coverage gets its
  own tiny, separately-gated Markdown block (never reusing
  `render_change_map`'s node/edge model, since intent coverage is a
  flat "surface: changed/unchanged" list, not a graph) -- but the
  *existing* Change Map is untouched; a `PotentialIntentGap` whose
  underlying node is already in the selected unit's Change Map does not
  add a second visual representation.

### Incremental review / metadata-change semantics

Intent Verification is **recomputed fresh, every run, from that run's
own already-fetched `PullRequestMetadata.title`/`.body`** -- exactly
like Change/Contract Intelligence, and exactly like Milestone K's
`base_sha` handling. Nothing about it is carried forward across Phase 7
incremental runs, and nothing about it is persisted keyed by "previous
intent text." A `synchronize` event that changes the PR description
between reviews simply produces a fresh `IntentVerificationReport` for
the new review run, anchored to that run's own current metadata --
there is no stale-binding risk because there is no cross-run intent
state at all. This also means Phase 7's own carry-forward semantics
(which findings/candidates get reused) are completely unaffected: Intent
Verification participates only in the same per-run evidence-into-prompt
mechanism J/K already use, never in what determines which candidates
get re-reviewed.

## 2. Domain model and architecture (final, post-correction)

`patchfrog/intent_verification/`:

- `domain.py` -- `IntentSourceKind` (5 values; **only `PR_TITLE`/`PR_BODY`
  are ever emitted as real `IntentEvidence`** -- `TEST_CHANGE` is
  defined/reserved for a future direct per-test signal, never emitted
  this milestone; see the correction round below for why the original
  draft's claim otherwise was wrong and got fixed), `IntentStrength`,
  `IntentEvidence`, `IntentClaim`, `IntentCoverageStatus`,
  `IntentGapReasonCode` (4 values, only `EXPECTED_SURFACE_UNCHANGED`
  constructed), `PotentialIntentGap`, `IntentCoverage`,
  `IntentVerificationReport`. `INTENT_VERIFICATION_VERSION = 1`,
  `MAX_INTENT_CLAIMS = 3`, `MAX_MAPPED_UNITS_PER_CLAIM = 2`.
- `extraction.py` -- `is_intent_evidence_sufficient` (the deterministic
  usability gate), `normalize_intent_text` (whitespace-collapse +
  sanitize + bound), `extract_claims_from_pr_metadata` (**body-precedence
  policy**: the PR body is authoritative whenever it is itself
  sufficient; title is used only as a fallback -- see the correction
  round below), `_extract_enumerated_goals` (structural, markdown-bullet-
  only, multi-claim splitting).
- `lexical.py` -- shared snake_case/camelCase/path-aware tokenizer
  (`tokenize`/`meaningful_tokens`), the basis for every bounded-overlap
  match in this package -- never embeddings, never a vector database.
- `mapping.py` -- `map_claim_to_units` (deterministic, bounded, ranked,
  tie-broken by unit id).
- `coverage.py` -- `derive_coverage_and_gaps` (the dedup logic: an
  affected-surface node already "owned" by an existing
  `ExpectedCompanionChange` -- matched by `qualified_name` for symbol
  nodes, by `file_path` for `TEST`-relation nodes -- is never also
  turned into a `PotentialIntentGap`; see the correction round below
  for the real dedup bug this closed).
- `story.py` -- `build_intent_story_prefix` (folded into `change_story`).
- `summary.py` -- `should_render_intent_coverage_summary`/
  `render_intent_coverage_summary` (the conditional user-facing block).
- `evidence.py` -- bounded `<intent_verification>` per-candidate prompt
  text.
- `telemetry.py` -- `IntentVerificationSummary`/`summarize_for_persistence`.
- `service.py` -- `build_intent_verification_report`, the one
  orchestration entry point -- deliberately synchronous/session-free
  (every input is already-computed, in-memory J/K evidence plus plain
  strings; this package never queries the repository graph itself).

Intent contradiction (spec section 12): **not implemented, explicitly
deferred**. Demonstrating "explicit intent states X, code establishes
structurally opposite Y" deterministically would require semantic
understanding of negation/opposite-behavior this index cannot provide
without guessing. The correction round instead added a **structural
precedence policy** (see below) that resolves the practical
"title/body disagree" case deterministically without attempting true
semantic contradiction detection.

## 2a. Correction round (post-review, before READY)

An external review of the first version of this PR flagged three real
gaps, all fixed before this READY declaration:

**1. Corpus below spec minimum.** The original corpus had 8 real-stack
cases; spec section 29 asks for at least 15 named scenarios. Added 7
more real-git-repo cases (`test_case_real_contract_stale_consumer_dedup`,
`test_case_refactor_intent_behavior_preserved_no_fabricated_gap`,
`test_case_error_handling_intent_missing_test_surface`,
`test_case_multiple_enumerated_goals_bounded_real_corpus`,
`test_case_title_body_contradiction_real_corpus`,
`test_case_meaningful_title_only_real_corpus`,
`test_case_vague_title_meaningful_body_real_corpus`) so all 15 named
scenarios now have direct, real-git/index/Change-Intelligence/Contract-
Intelligence-backed proof -- see section 4 below for the full mapping.

**2. No title/body precedence policy.** `extract_claims_from_pr_metadata`
originally evaluated title and body *independently*, so two
sufficiently-specific but disagreeing statements could produce two
unrelated claims with no defined relationship. Fixed with the
reviewer's own suggested Option B: **the PR body is authoritative
whenever it is itself sufficient evidence; the title is used only as a
fallback when the body is absent or insufficient.** This is a
deterministic structural policy, never semantic contradiction
detection. One consequence, also required by the correction: title and
body can no longer simultaneously produce two claims for the same PR
(`test_never_emits_both_title_and_body_claims_simultaneously`), except
for the one legitimate case of an explicitly enumerated bullet/numbered
list in the body (`test_enumerated_body_goals_split_into_separate_claims`,
`test_enumerated_goals_bounded_to_max_intent_claims`) -- structural
detection only, never NLP sentence splitting. Direct regression test
for the disagreement case itself:
`test_extract_claims_body_precedence_resolves_disagreement`
(unit-level) and `test_case_title_body_contradiction_real_corpus`
(real corpus).

**3. `TEST_CHANGE` was overclaimed.** The original draft's docs/PR body
said "Supported intent sources: PR_TITLE, PR_BODY, TEST_CHANGE," but
`extract_claims_from_pr_metadata` never actually constructed a
`TEST_CHANGE`-kind `IntentEvidence` object -- the real "test surface"
signal was always the existing companion-reference dedup mechanism, a
different thing entirely. Corrected throughout (`domain.py`'s
`IntentSourceKind` docstring, `docs/intent-verification.md`, this
document) to say plainly: **two emitted explicit sources (`PR_TITLE`/
`PR_BODY`); test-surface signal is supporting repository evidence via
companion reference, not a distinct emitted source.** The smallest
truthful fix, per the review's own explicit preference -- no fake
`TEST_CHANGE` evidence was added solely to satisfy the enum.

**A fourth, real bug was found *while building* the corrected corpus**
(not flagged by the external review, caught by writing case 3's real
Contract Intelligence integration test): the original `coverage.py`
only checked `ref.qualified_name` against existing companions'
`expected_qualified_name` before constructing a `PotentialIntentGap` --
missing two real duplication paths: (a) a `TEST`-relation
`AffectedSymbolRef` always has `qualified_name=None` (only `file_path`
is set), so it was never actually protected by that check at all; (b)
more subtly, when a real caller-direction affected-surface node is
lexically relevant, J's own `CALLER_NOT_UPDATED` companion heuristic
*already* tracks that exact same edge unconditionally -- so
`PotentialIntentGap` was silently duplicating it. Fixed by checking
both `qualified_name` (symbol nodes) and `file_path` (`TEST` nodes)
against the mapped unit's existing companions before ever constructing
a gap. This also clarified `PotentialIntentGap`'s true scope: it now
only ever fires for a *callee*-direction affected-surface node (J's
companions only track callers) or a 2-hop `INDIRECTLY_AFFECTED` node
(J's companions only look at depth-1) -- both real, previously-uncovered
signals, documented explicitly in `docs/intent-verification.md`'s
`PotentialIntentGap` section. `test_case_one_real_affected_path_forgotten`
was rewritten to use a callee edge specifically, with an explanatory
comment for why a caller edge would now (correctly) be redundant.

**Versioning decision**: `INTENT_VERIFICATION_VERSION` stays at `1`.
This PR has not merged; `1` is defined as the final, corrected
semantics shipped by this PR, not the pre-correction draft -- per the
review's own explicit guidance that this is acceptable pre-merge.
`REVIEW_PROMPT_VERSION`/`TELEMETRY_SCHEMA_VERSION`/other version
constants are unaffected by this correction (no prompt-template-shape
or telemetry-export-shape change was made).

## 4. Corpus results (post-correction: all 15 spec scenarios, real stack)

`tests/integration/test_intent_verification_corpus.py` -- **15 tests**,
real git repository (real commits, a genuine base/head diff), real
indexing, real diff-driven candidate generation, real
`build_change_intelligence_report` for real `ChangeUnit`s (and, for
case 3, real `build_contract_intelligence_report` for real `ContractDelta`s/
stale consumers), real `build_intent_verification_report`. **15/15
pass.** Zero LLM involvement (structurally proven by a dedicated test
in the same file).

Every one of spec section 29's 15 named scenarios now has **direct
real-stack proof** (not a unit-test stand-in):

| Spec scenario (section 29) | Corpus test | Result |
|---|---|---|
| 1. explicit intent + complete implementation | `test_case_complete_implementation_no_gap` | claim mapped, `SUPPORTED`, zero gaps |
| 2/8. one real affected path forgotten (retry scheduling) | `test_case_one_real_affected_path_forgotten` | `PARTIAL_EVIDENCE`, 1 real `PotentialIntentGap` naming `schedule_retry` (a *callee* edge -- deliberately not a caller edge, which is already companion-owned; see section 2a) |
| 3. real stale Contract Intelligence consumer -- dedup | `test_case_real_contract_stale_consumer_dedup` | real K `ContractDelta` + real K/J stale-consumer companions referenced via `relevant_companion_candidates`; **zero** `PotentialIntentGap` objects for that surface |
| 4. vague title | `test_case_vague_title_skipped` | zero claims, zero coverage, zero gaps |
| 5. docs-only PR with explicit documentation intent | `test_case_docs_only_pr_with_explicit_intent_no_code_gap_noise` | zero gaps (no symbol-level surface to spuriously flag) |
| 6. refactor intent, behavior preserved | `test_case_refactor_intent_behavior_preserved_no_fabricated_gap` | 1 claim (not vague), zero fabricated gaps |
| 7. error-handling intent + graph-linked test surface missing | `test_case_error_handling_intent_missing_test_surface` | real `TEST_NOT_UPDATED` companion referenced; zero duplicate gap for the same test surface |
| 9. explicit intent but unrelated ChangeUnits | `test_case_explicit_intent_but_unrelated_change_units_not_mapped` | `INSUFFICIENT_EVIDENCE`, zero gaps -- the unrelated unit is never mapped |
| 10. multiple explicit goals, bounded | `test_case_multiple_enumerated_goals_bounded_real_corpus` | 3 claims from 3 sufficient bullets (a 4th, "typo", correctly dropped), `<= MAX_INTENT_CLAIMS` |
| 11. PR title/body contradiction | `test_case_title_body_contradiction_real_corpus` | exactly 1 claim, from the body, per the deterministic precedence policy -- maps against the real graph on the body's own terms |
| 12. no PR body, meaningful title only | `test_case_meaningful_title_only_real_corpus` | 1 claim, source `PR_TITLE`, maps against the real graph |
| 13. meaningful body, vague title | `test_case_vague_title_meaningful_body_real_corpus` | 1 claim, source `PR_BODY`, maps against the real graph |
| 14. metadata absent | `test_case_no_pr_metadata_is_a_no_op` | zero claims |
| 15. already-updated expected surface | `test_case_already_updated_expected_surface_no_false_positive_gap` | zero gaps (the affected node itself was genuinely part of the diff) |

**Negative/false-positive tests (spec section 31)**: vague PR title
(case 4), docs-only PR (case 5), refactor with behavior preserved
(case 6), unrelated unchanged consumer / unmappable claim (case 9),
already-updated expected surface (case 15), no PR metadata (case 14),
plus unit-level irrelevant-affected-surface-never-a-gap and
dedup-not-a-duplicate-candidate proofs. All pass -- zero false-positive
gap candidates anywhere in the corpus.

**Pipeline integration** (not just isolated service calls):
`tests/integration/test_intent_verification_review_pipeline.py` -- 2
tests, driving the real `PullRequestReviewService.review_local` end to
end (scripted `FakeLLMProvider`, never live, using the same callee-edge
fixture as corpus case 2/8): one proves counts/Intent-Story-prefix
persist correctly onto the real `review_runs` row; one proves
`title=None, body=None` (every review before this milestone) is a
complete no-op.

**Unit coverage**: `test_intent_verification_extraction.py` (17 tests --
every spec section 5 example, normalization/bounding, body-precedence
policy including the disagreement case, enumerated-goal splitting and
its bound, never-both-simultaneously proof, deterministic claim id,
verbatim-statement proof), `test_intent_verification_lexical.py` (6
tests), `test_intent_verification_mapping_coverage.py` (9 tests --
unrelated-never-mapped, related-mapped-with-shared-terms, bound
enforcement, SUPPORTED/INSUFFICIENT_EVIDENCE/PARTIAL_EVIDENCE for every
case including the dedup case), `test_intent_verification_summary.py`
(5 tests -- eligibility gating, no-percentage proof),
`test_intent_verification_versioning.py` (10 tests). **Total: 64 new
tests** (up from the original 52).

## 5. Success metrics (controlled-corpus evidence only, spec section 30)

- **Usable-intent gating precision**: all 6 spec-listed insufficient
  examples correctly rejected; all 4 spec-listed sufficient examples
  correctly accepted (`test_intent_verification_extraction.py`).
- **Mapping precision/recall**: 1/1 unrelated unit correctly left
  unmapped (never a false positive); every related unit correctly
  mapped via real shared terms (never a false negative) across the
  full 15-scenario real-git-repo corpus.
- **Gap precision/recall**: every corpus case with a real forgotten
  path (cases 2/8) produced the expected gap; 0 false-positive gaps
  across the other 13 corpus cases, including two cases specifically
  designed to trigger a duplicate if the dedup logic were wrong
  (case 3's real Contract Intelligence stale consumer, case 7's real
  test-surface companion).
- **False-positive rate on complete implementations**: 0/1.
- **False-positive rate on vague intent**: 0/1 (nothing is even
  evaluated once the sufficiency gate fails).
- **False-positive rate on refactor intent**: 0/1 (case 6).
- **Duplicate-evidence suppression result**: proven at both the unit
  level (`test_missing_companion_dedup_not_a_second_gap_object`) and
  the real-stack level (corpus cases 3 and 7) -- a `MISSING` companion,
  from either J or K, never produces a second `PotentialIntentGap` for
  the same surface.
- **Title/body precedence result**: proven at both levels (unit:
  `test_extract_claims_body_precedence_resolves_disagreement`; corpus:
  `test_case_title_body_contradiction_real_corpus`) -- body always wins
  when sufficient, deterministically, never two competing claims.
- **Multi-claim bound result**: proven at both levels -- never exceeds
  `MAX_INTENT_CLAIMS = 3`, and only splits on real structural
  enumeration (markdown bullets), never NLP sentence splitting.
- **Extra provider calls**: **0** (structurally proven).
- **Prompt/token delta**: `REVIEW_PROMPT_VERSION` 5 -> 6 (new optional
  `<intent_verification>` section, empty/byte-identical for every
  candidate except one that's part of a mapped ChangeUnit).

## 6. Gates

All run against the real changes on this branch, post-correction,
2026-09-05:

| Gate | Result |
|---|---|
| `git diff --check` | clean, no whitespace/conflict-marker errors |
| `ruff check .` | All checks passed! |
| `mypy . --strict` | Success: no issues found in 475 source files |
| `pytest` (full suite, real Postgres + Redis, migrated to head `0020_intent_verification`) | **1521 passed, 0 failed** (baseline before this milestone: 1457; before this correction: 1509) |
| Alembic single head | `alembic heads` -> `0020_intent_verification (head)`; real `alembic upgrade head` against Postgres succeeded cleanly (unchanged by this correction -- no schema change) |
| Docker API image build | rebuilt clean post-correction (`patchfrog-api:l-check2`) |
| Docker worker image build | rebuilt clean post-correction (`patchfrog-worker:l-check2`) |
| Celery task registration | `tests/integration/test_celery_task_registration.py` -- 1 passed (subprocess-isolated) |
| Intent Verification tests | **64 new tests** (17 extraction + 6 lexical + 9 mapping/coverage + 5 summary + 10 versioning unit tests; 15 corpus + 2 real-pipeline integration tests) -- all pass |
| Change/Contract Intelligence / Context Engine / review prompt-versioning / telemetry collector-versioning / publishing / carried-forward / Change Map tests | included in the full run above, no regressions |
| Docs links | `docs/intent-verification.md` -- referenced module paths checked to exist |
| Tracked-file / PR-diff secret scan | every changed/new file scanned for common credential shapes -- no matches |

Provider calls added by this milestone: **0** (structurally proven,
`test_intent_verification_never_calls_a_provider`). No Gemini call, no
Anthropic call, no OpenAI call, no Cloud/dashboard work.
