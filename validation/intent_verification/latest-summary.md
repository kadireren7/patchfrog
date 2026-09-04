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

**Implemented this milestone**: `PR_TITLE`, `PR_BODY` (both EXPLICIT --
zero new GitHub calls, already-fetched live metadata) and `TEST_CHANGE`
(SUPPORTING -- zero new plumbing, reuses Change Intelligence's own test
relationship).

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
extraction logic only ever produces `PR_TITLE`/`PR_BODY`/`TEST_CHANGE`
evidence.

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

## 2. Domain model and dedup design (as implemented)

- `IntentSourceKind`: `PR_TITLE`, `PR_BODY`, `LINKED_ISSUE`,
  `COMMIT_MESSAGE`, `TEST_CHANGE` (5 values, taxonomy completeness) --
  only `PR_TITLE`/`PR_BODY`/`TEST_CHANGE` ever produced.
- `IntentStrength`: `EXPLICIT` (PR title/body) / `SUPPORTING` (test
  change) -- supporting evidence never independently creates a claim
  (spec section 2's hard requirement), only strengthens/weakens mapping
  confidence for an already-EXPLICIT claim.
- `IntentEvidence`: source kind + a short source identifier (`"title"`
  or `"body"`) + bounded normalized text (never the full raw PR body --
  see Persistence section) + strength.
- `IntentClaim`: deterministic id (`sha256(source_kind + normalized_statement)[:16]`),
  the normalized statement itself (sanitized/bounded, never a
  paraphrase -- spec section 6: preserving the sanitized explicit
  statement verbatim is acceptable and is exactly what's implemented),
  its source evidence, strength. `MAX_INTENT_CLAIMS = 3`.
- `IntentCoverageStatus`: `SUPPORTED` / `PARTIAL_EVIDENCE` /
  `INSUFFICIENT_EVIDENCE` -- never a percentage.
- `IntentCoverage`: claim id, mapped `ChangeUnit` ids (bounded to
  `MAX_MAPPED_UNITS_PER_CLAIM = 2`), the changed candidates that
  matched (`covered_surfaces`), the *existing* J affected-surface nodes
  that matched but weren't changed (`potentially_uncovered_surfaces`),
  the *existing* K `ContractDelta`s belonging to mapped units
  (`relevant_contract_deltas`), and -- the dedup mechanism -- the
  *existing* `ExpectedCompanionChange` instances (from either J's
  companions or K's stale consumers) that are lexically relevant to
  this claim (`relevant_companion_candidates`, references only, never
  copies).
- `IntentGapReasonCode`: `EXPECTED_SURFACE_UNCHANGED`,
  `RELATED_PATH_UNCHANGED`, `CONTRACT_CONSUMER_STALE`,
  `EXPECTED_TEST_SURFACE_MISSING` (4 values, spec section 11's full
  taxonomy, kept for documentation). **Only `EXPECTED_SURFACE_UNCHANGED`
  is ever used to construct a real `PotentialIntentGap`** -- the other
  three describe exactly the case where J/K *already* produced a
  `MISSING` `ExpectedCompanionChange` for the same surface, which (per
  the dedup rule above) is surfaced via `relevant_companion_candidates`
  instead of a second object. This is the explicit resolution of spec
  section 14's audit question: reuse, don't duplicate.
- `PotentialIntentGap`: claim id, change unit id, the `AffectedSymbolRef`
  that's relevant-but-unchanged, reason code (always
  `EXPECTED_SURFACE_UNCHANGED`), evidence. **Never auto-published** --
  same discipline as every other J/K candidate.
- Intent contradiction (spec section 12): **not implemented, explicitly
  deferred**. Demonstrating "explicit intent states X, code establishes
  structurally opposite Y" deterministically would require semantic
  understanding of negation/opposite-behavior this index cannot
  provide without guessing -- precision over checklist completion.

## 3. Domain model and architecture (as implemented)

`patchfrog/intent_verification/`:

- `domain.py` -- `IntentSourceKind` (5 values, 3 produced),
  `IntentStrength`, `IntentEvidence`, `IntentClaim`, `IntentCoverageStatus`,
  `IntentGapReasonCode` (4 values, 1 constructed), `PotentialIntentGap`,
  `IntentCoverage`, `IntentVerificationReport`. `INTENT_VERIFICATION_VERSION = 1`,
  `MAX_INTENT_CLAIMS = 3`, `MAX_MAPPED_UNITS_PER_CLAIM = 2`.
- `extraction.py` -- `is_intent_evidence_sufficient` (the deterministic
  usability gate), `normalize_intent_text` (whitespace-collapse +
  sanitize + bound), `extract_claims_from_pr_metadata`.
- `lexical.py` -- shared snake_case/camelCase/path-aware tokenizer
  (`tokenize`/`meaningful_tokens`), the basis for every bounded-overlap
  match in this package -- never embeddings, never a vector database.
- `mapping.py` -- `map_claim_to_units` (deterministic, bounded, ranked,
  tie-broken by unit id).
- `coverage.py` -- `derive_coverage_and_gaps` (the dedup logic: only
  `EXPECTED_SURFACE_UNCHANGED` ever becomes a real `PotentialIntentGap`;
  existing J/K `MISSING` companions are referenced, never duplicated).
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

## 4. Corpus results

`tests/integration/test_intent_verification_corpus.py` -- 8 tests, real
git repository (two real commits, a genuine base/head diff), real
indexing, real diff-driven candidate generation, real
`build_change_intelligence_report` for real `ChangeUnit`s, real
`build_intent_verification_report`. **8/8 pass.** Zero LLM involvement
(structurally proven by a dedicated test in the same file).

| Spec scenario (section 29) | Corpus test | Result |
|---|---|---|
| 1. explicit intent + complete implementation | `test_case_complete_implementation_no_gap` | claim mapped, `SUPPORTED`, zero gaps |
| 2/8. explicit intent + one real affected path forgotten (retry worker) | `test_case_one_real_affected_path_forgotten` | `PARTIAL_EVIDENCE`, 1 real `PotentialIntentGap` naming `run_retry`, reason `EXPECTED_SURFACE_UNCHANGED` |
| 3. stale contract consumer -- canonical evidence, no duplicate | `test_missing_companion_dedup_not_a_second_gap_object` (unit test) | `PARTIAL_EVIDENCE`, **zero** `PotentialIntentGap` objects, the existing `MISSING` companion referenced via `relevant_companion_candidates` |
| 4. vague title | `test_case_vague_title_skipped` | zero claims, zero coverage, zero gaps |
| 5. docs-only PR with explicit documentation intent | `test_case_docs_only_pr_with_explicit_intent_no_code_gap_noise` | zero gaps (no symbol-level surface to spuriously flag) |
| 6/7/10/11/13 | covered by unit tests (sufficiency gate examples, multi-claim bound, title-only/body-only cases) rather than a dedicated corpus fixture each -- see section 5 |
| 9. explicit intent but unrelated ChangeUnits | `test_case_explicit_intent_but_unrelated_change_units_not_mapped` | `INSUFFICIENT_EVIDENCE`, zero gaps -- the unrelated unit is never mapped |
| 12. no PR body, meaningful title only | `test_extract_claims_no_body_meaningful_title_only` (unit test) | 1 claim, source `PR_TITLE` |
| 14. metadata absent | `test_case_no_pr_metadata_is_a_no_op` | zero claims |
| 15. already-updated expected surface | `test_case_already_updated_expected_surface_no_false_positive_gap` | zero gaps (the affected node itself was genuinely part of the diff) |

**Negative/false-positive tests (spec section 31)**: vague PR title
(case 4), docs-only PR (case 5), unrelated unchanged consumer /
unmappable claim (case 9), already-updated expected surface (case 15),
no PR metadata (case 14), irrelevant-affected-surface-never-a-gap (unit
test), dedup-not-a-duplicate-candidate (unit test). All pass -- zero
false-positive gap candidates anywhere in the corpus.

**Pipeline integration** (not just isolated service calls):
`tests/integration/test_intent_verification_review_pipeline.py` -- 2
tests, driving the real `PullRequestReviewService.review_local` end to
end (scripted `FakeLLMProvider`, never live): one proves counts/Intent-
Story-prefix persist correctly onto the real `review_runs` row; one
proves `title=None, body=None` (every review before this milestone) is
a complete no-op.

**Unit coverage**: `test_intent_verification_extraction.py` (12 tests --
every spec section 5 example, normalization/bounding, both-sources/
title-only/body-only/neither, deterministic claim id, verbatim-statement
proof), `test_intent_verification_lexical.py` (6 tests -- snake_case/
camelCase/path splitting, stopword/short-token filtering, prose<->identifier
overlap), `test_intent_verification_mapping_coverage.py` (9 tests --
unrelated-never-mapped, related-mapped-with-shared-terms, bound
enforcement, SUPPORTED/INSUFFICIENT_EVIDENCE/PARTIAL_EVIDENCE for every
case including the dedup case), `test_intent_verification_summary.py`
(5 tests -- eligibility gating, no-percentage proof), `test_intent_verification_versioning.py`
(10 tests).

## 5. Success metrics (controlled-corpus evidence only, spec section 30)

- **Usable-intent gating precision**: all 6 spec-listed insufficient
  examples correctly rejected; all 4 spec-listed sufficient examples
  correctly accepted (`test_intent_verification_extraction.py`).
- **Mapping precision/recall**: 1/1 unrelated unit correctly left
  unmapped (never a false positive); 1/1 related unit correctly mapped
  via real shared terms (never a false negative) across both the unit
  tests and the real-git-repo corpus.
- **Gap precision/recall**: 1/1 corpus case with a real forgotten path
  produced the expected gap; 0/6 corpus cases without a real forgotten
  path produced a false-positive gap.
- **False-positive rate on complete implementations**: 0/1.
- **False-positive rate on vague intent**: 0/1 (nothing is even
  evaluated once the sufficiency gate fails).
- **Duplicate-evidence suppression result**: proven directly
  (`test_missing_companion_dedup_not_a_second_gap_object`) -- a `MISSING`
  companion never produces a second `PotentialIntentGap`.
- **Extra provider calls**: **0** (structurally proven).
- **Prompt/token delta**: `REVIEW_PROMPT_VERSION` 5 -> 6 (new optional
  `<intent_verification>` section, empty/byte-identical for every
  candidate except one that's part of a mapped ChangeUnit).

## 6. Gates

All run against the real changes on this branch, 2026-09-04:

| Gate | Result |
|---|---|
| `git diff --check` | clean, no whitespace/conflict-marker errors |
| `ruff check .` | All checks passed! |
| `mypy . --strict` | Success: no issues found in 475 source files |
| `pytest` (full suite, real Postgres + Redis, migrated to head `0020_intent_verification`) | **1509 passed, 0 failed** (baseline before this milestone: 1457) |
| Alembic single head | `alembic heads` -> `0020_intent_verification (head)`; real `alembic upgrade head` against Postgres succeeded cleanly |
| Docker API image build | `docker build --target api` -> `Successfully tagged patchfrog-api:l-check` |
| Docker worker image build | `docker build --target worker` -> `Successfully tagged patchfrog-worker:l-check` |
| Celery task registration | `tests/integration/test_celery_task_registration.py` -- 1 passed (subprocess-isolated) |
| Intent Verification tests | 52 new tests (12 extraction + 6 lexical + 9 mapping/coverage + 5 summary + 10 versioning unit tests; 8 corpus + 2 real-pipeline integration tests) -- all pass |
| Change/Contract Intelligence / Context Engine / review prompt-versioning / telemetry collector-versioning / publishing / carried-forward / Change Map tests | included in the full run above, no regressions |
| Docs links | `docs/intent-verification.md` -- referenced module paths checked to exist |
| Tracked-file / PR-diff secret scan | every changed/new file scanned for common credential shapes -- no matches |

Provider calls added by this milestone: **0** (structurally proven,
`test_intent_verification_never_calls_a_provider`). No Gemini call, no
Anthropic call, no OpenAI call, no Cloud/dashboard work.
