# Intent Verification Foundation

`patchfrog/intent_verification/` extends `patchfrog/change_intelligence/`
(Milestone J) and `patchfrog/contract_intelligence/` (Milestone K) with a
deterministic answer to a narrower, harder question: **what is this PR
explicitly trying to accomplish, and does the actual change cover the
relevant implementation surface?** It is not a third parallel
intelligence stack -- see "Reuse, not duplication" below. This document
does not introduce a new phase number, and it does not change what
PatchFrog is: **a reviewer/verifier that finds fewer, harder,
evidence-backed problems** -- never speculative requirements
generation, never a numeric PR score, never automatic publication from
a heuristic.

**PatchFrog does not invent requirements. If explicit intent or
repository evidence is insufficient, Intent Verification fails closed
and produces no gap candidate.**

## What PatchFrog considers intent

An `IntentClaim` is produced only from **explicit** PR text (title/body)
that passes a deterministic sufficiency gate -- never from a file name,
a branch name, a source comment, unrelated README prose, or an LLM
guess. Test-file changes are **supporting** evidence only: they can
strengthen or weaken an already-explicit claim's coverage evidence, but
can never independently create a claim.

## Supported intent sources

**Explicit sources (can independently establish an `IntentClaim`):**

- **PR title** -- Already fetched live for every real review (the same
  `PullRequestMetadata` fetch Milestone K already used for `base_sha`);
  no new GitHub call.
- **PR body** -- Same fetch, same free reuse. Never persisted durably
  elsewhere in this codebase before this milestone (`PullRequestModel`
  has no `body` column) -- Intent Verification reads it live, at review
  time, exactly once per run.

**Supporting repository evidence (never independently creates a claim):**

- **Test surfaces from Change Intelligence** -- not a distinct
  `IntentEvidence` object. When an already-existing `TEST_NOT_UPDATED`
  `ExpectedCompanionChange` (Change Intelligence's own test-relationship
  evidence, via `IndexedFileModel.is_test`/`likely_tests_for_file`)
  belongs to a claim's mapped `ChangeUnit`, it's referenced through
  `IntentCoverage.relevant_companion_candidates` -- the same dedup
  mechanism used for every other J/K companion (see "Reuse, not
  duplication" below). `IntentSourceKind.TEST_CHANGE` is defined on the
  enum for a future, more direct per-test signal but is **not emitted**
  by this milestone's extraction path -- see
  `patchfrog.intent_verification.domain.IntentSourceKind`'s own
  docstring for the precise distinction.

### Deferred (and why)

- **Linked issues** -- PatchFrog has no issue-body-fetch capability at
  all today. Adding one would mean a genuinely new GitHub API call, new
  error handling for a private/deleted/cross-repo issue, and a new
  installation-permission surface to reason about -- "expand scope
  substantially," which spec section 18 explicitly permits deferring.
- **Commit messages** -- `patchfrog.github.client.GitHubClient` has no
  "list PR commits" method; fetching them would be new plumbing, not
  free reuse. Deferred per spec section 19's explicit instruction.

`IntentSourceKind` keeps all five values from the milestone brief for
forward documentation, but this milestone's extraction logic only ever
emits `PR_TITLE`/`PR_BODY` `IntentEvidence`. Full reasoning:
`validation/intent_verification/latest-summary.md` section 1.

## Title/body precedence (never semantic contradiction detection)

When both title and body are independently sufficient, **the PR body is
authoritative** -- title is used only as a fallback when the body is
absent or insufficient. This is a deliberate, deterministic structural
policy (spec's own suggested Option B), not an attempt at semantic
contradiction detection (which would require guessing whether two
statements actually disagree). One consequence: title and body never
simultaneously produce two separate, potentially-conflicting claims for
the same PR -- see
`test_extract_claims_body_precedence_resolves_disagreement` for a direct
regression proof.

The one deterministic exception that legitimately preserves more than
one claim from a single source: the body explicitly enumerates goals as
a markdown bullet/numbered list (checked structurally, before whitespace
collapsing destroys the line boundaries a list depends on -- never NLP
sentence splitting). Each individually-sufficient bullet becomes its own
claim, bounded to `MAX_INTENT_CLAIMS = 3`; an insufficient bullet is
dropped, never forced into a claim. Prose without that explicit
structure is always treated as one conservative combined claim.

## The sufficiency gate

`patchfrog.intent_verification.extraction.is_intent_evidence_sufficient`
is a deterministic, never-LLM-delegated gate. It rejects:

- an exact match against a curated placeholder list (`"fix"`,
  `"cleanup"`, `"changes"`, `"WIP"`, `"refactor stuff"`, `"try again"`,
  ...) -- checked against the *entire* normalized statement, never a
  substring, so a real sentence that happens to contain the word "fix"
  is never penalized;
- text with too few real content words (stopwords removed): at least 3
  when a recognized behavioral verb is present (`prevent`, `ensure`,
  `reject`, `allow`, `create`, `make`, ... -- a deliberately non-exhaustive
  curated list), or at least 7 without one (demanding more content to
  compensate, rather than requiring an exhaustive verb dictionary).

Title and body are evaluated independently -- a sufficient title alone
is usable even with an empty/vague body, and vice versa.

## IntentClaim

`normalized_statement` is the sanitized, whitespace-collapsed, bounded
(500 chars) source text itself -- **never a paraphrase, never an LLM
summary**. `id` is `sha256(source_kind + normalized_statement)[:16]` --
deterministic, so re-running the same review (or a Phase 7 incremental
re-review) always produces the same claim id. Bounded to
`MAX_INTENT_CLAIMS = 3`; in practice this is exactly 1 claim (body-or-
title, per the precedence rule above) unless the body explicitly
enumerates goals as a bullet/numbered list, in which case up to 3.

## Reuse, not duplication

Nothing here re-derives what changed or what it affects:

- **ChangeUnit mapping** (`patchfrog.intent_verification.mapping`)
  operates on already-built `ChangeUnit`s -- bounded lexical token
  overlap only (snake_case/camelCase/path-aware tokenization, no
  embeddings, no vector database), capped at
  `MAX_MAPPED_UNITS_PER_CLAIM = 2`. An unrelated unit with zero shared
  tokens is never mapped.
- **Expected/relevant surface** comes entirely from J's `AffectedSymbolRef`
  (already computed by `derive_affected_surface`) and K's `ContractDelta`
  -- Intent Verification never invents an affected surface from prose.
  It only *filters* the surface J/K already computed by lexical
  relevance to an explicit claim.
- **Dedup against J/K** (spec section 14): when J/K already flag a
  missing surface as a `MISSING` `ExpectedCompanionChange` (a
  `CALLER_NOT_UPDATED`/`TEST_NOT_UPDATED`/`CONTRACT_CONSUMER_NOT_UPDATED`),
  Intent Verification never constructs a second, near-duplicate
  candidate for the same surface -- it *references* the existing object
  (`IntentCoverage.relevant_companion_candidates`). `PotentialIntentGap`
  (a genuinely new type) is reserved for the one new signal this
  milestone adds: a real `AffectedSymbolRef` that J/K never flagged as
  missing (they have no concept of relevance to explicit intent) but
  which is lexically relevant to a claim and wasn't changed.
- **No second diagram, no second Change Map**: the conditional Intent
  Coverage summary is its own tiny, separately-gated Markdown block
  (a flat "surface: changed/unchanged" list), never a re-render of
  `render_change_map`'s node/edge model.

## Implementation coverage

`IntentCoverage.status` is one of `SUPPORTED` / `PARTIAL_EVIDENCE` /
`INSUFFICIENT_EVIDENCE` -- **never a percentage**:

- `INSUFFICIENT_EVIDENCE` -- the claim couldn't be mapped to any real
  ChangeUnit (spec section 8: "If mapping is ambiguous: leave the claim
  unmapped").
- `PARTIAL_EVIDENCE` -- mapped, but at least one lexically-relevant
  affected-surface node remains unchanged, or a relevant J/K companion
  candidate is still `MISSING`.
- `SUPPORTED` -- mapped, with no unresolved gap.

## PotentialIntentGap

Constructed only when a real `AffectedSymbolRef` (already computed by
J, `DIRECTLY_DEPENDENT`/`INDIRECTLY_AFFECTED`) shares a meaningful
lexical token with the claim, was not itself part of the diff, **and is
not already owned by an existing `ExpectedCompanionChange`** for the
same unit (matched by `qualified_name` for symbol-level nodes, or by
`file_path` for `TEST`-relation nodes, which carry no `qualified_name`
at all). Reason code is always `EXPECTED_SURFACE_UNCHANGED`. The full
spec section 11 taxonomy (`RELATED_PATH_UNCHANGED`,
`CONTRACT_CONSUMER_STALE`, `EXPECTED_TEST_SURFACE_MISSING`) is kept on
`IntentGapReasonCode` for documentation, but the latter three describe
cases already covered by an existing J/K `ExpectedCompanionChange` (see
Reuse section above) -- **never auto-published**, exactly like every
other J/K candidate.

**A consequence worth being explicit about**: J's own `CALLER_NOT_UPDATED`
companion heuristic already tracks *every* real caller of *any* changed
symbol, unconditionally -- so a `DIRECTLY_DEPENDENT` affected-surface
node reached via a caller edge is, in practice, always already
companion-owned, and never produces a `PotentialIntentGap`. The gap
mechanism's genuinely novel contribution is therefore two-fold: (1) a
*callee*-direction `DIRECTLY_DEPENDENT` node (something the changed
symbol itself calls) -- J's companions only look at callers, never
callees, so this is real, uncovered evidence; and (2) an
`INDIRECTLY_AFFECTED` (2-hop) node, which J's own companion heuristic
never reaches at all (it only inspects depth-1 callers of the exact
changed symbol). Both are proven directly in the corpus
(`test_case_one_real_affected_path_forgotten` uses a callee edge
specifically, with a comment explaining why a caller edge would have
been a redundant test).

## Intent contradiction (deferred)

Not implemented. Demonstrating "explicit intent states X, code
establishes structurally opposite Y" deterministically would require
semantic understanding of negation/opposite-behavior this index cannot
provide without guessing -- precision over checklist completion (spec
section 12 explicitly permits deferring this).

## Review pipeline integration

Computed once per run, right after Change/Contract Intelligence
(consuming their already-built evidence directly -- no repository-graph
query of its own, no database session at all). A third optional
`<intent_verification>` prompt section (`REVIEW_PROMPT_VERSION` 5 -> 6),
attached only to the exact candidate that is part of a claim's mapped
ChangeUnit -- empty for every other candidate, including every candidate
on a PR with no usable intent at all. `REVIEW_POLICY_VERSION`/
`REVIEW_ENGINE_VERSION`/`CHANGE_INTELLIGENCE_VERSION`/
`CONTRACT_INTELLIGENCE_VERSION` are **not** bumped -- nothing about
finding survival, orchestration, or those packages' own logic changed.
**No new agent role** -- Correctness and Security remain the only
specialists.

**Zero additional provider calls.** Structurally proven
(`test_intent_verification_never_calls_a_provider`): no `LLMProvider`
import anywhere in `patchfrog/intent_verification/`.

## Change Story integration

`patchfrog.intent_verification.story.build_intent_story_prefix` produces
at most one bounded sentence ("Intent: <claim>."), prepended to the
existing Change/Contract Story text -- never a separate publication
block, never a separate persisted column. Empty unless a usable claim
exists (which already implies it passed the sufficiency gate -- no
additional gate needed here).

## Conditional Intent Coverage summary

`patchfrog.intent_verification.summary.should_render_intent_coverage_summary`
is a deterministic eligibility gate (never an LLM judgment call): shown
only when a mapped claim's total surface count (changed + unchanged) is
at least 2 -- a single-surface claim adds nothing beyond the Change
Story sentence. Format is a flat, bounded Markdown list
(`### Intent coverage` / `- surface: changed` / `- surface: unchanged`)
-- **never a percentage, never a confidence score, never a
green/red badge**.

## Incremental review / metadata-change semantics

Intent Verification is **recomputed fresh, every run, from that run's
own already-fetched PR title/body** -- exactly like Change/Contract
Intelligence's own `base_sha` handling. Nothing is carried forward
across Phase 7 incremental runs, and nothing is persisted keyed by
"previous intent text." A `synchronize` event that changes the PR
description between reviews simply produces a fresh
`IntentVerificationReport` for the new review run; Phase 7's own
carry-forward semantics (which findings/candidates get reused) are
completely unaffected, since Intent Verification only participates in
the same per-run evidence-into-prompt mechanism J/K already use.

## Persistence

`review_runs` gained six nullable-default columns (migration
`0020_intent_verification`): `intent_claim_count`,
`intent_source_kind_counts`, `mapped_intent_claim_count`,
`intent_gap_candidate_count`, `intent_coverage_summary_rendered`,
`intent_coverage_summary_text`. **No new text column for the Intent
Story** -- it's folded into the existing `change_story` column.
`intent_coverage_summary_text` IS a new, dedicated text column (unlike
the Change/Contract Map, the Intent Coverage block is its own separate
publication section, not a re-render of an existing one) -- needed
because publication (`apps/worker/tasks/publish_review.py`) runs as a
separate, independently-retriable Celery task from review generation,
the same justification precedent Milestone J/K already established for
`change_map_text`. Neither PR title/body raw text, nor commit history,
nor source/diff bodies are ever persisted by this package.

## Telemetry and versioning

`patchfrog.telemetry.domain.IntentVerificationTelemetry` (counts only --
no PR title/body text, no claim statements, no Intent Story/Coverage
prose) is a new field on `ReviewTelemetrySnapshot`. Because
`snapshot_to_dict` exports every dataclass field via `dataclasses.asdict`,
this is a real exported-JSON-shape change, so `TELEMETRY_SCHEMA_VERSION`
is bumped 3 -> 4 (applying the Milestone J correction / Milestone K
precedent proactively, not repeating the original oversight). Historical
rows export `intent_verification` with explicit zero/default values.

`INTENT_VERIFICATION_VERSION = 1` is introduced as this package's own
semantic-identity version. `CHANGE_INTELLIGENCE_VERSION`/
`CONTRACT_INTELLIGENCE_VERSION` are **unchanged** -- neither package's
own grouping/affected-surface/delta/companion logic changed; only a
consuming package was added.

## Limitations

- Lexical mapping is bounded token overlap, not semantic understanding
  -- a claim phrased with entirely different vocabulary than the
  relevant symbols/files will not map, and stays `INSUFFICIENT_EVIDENCE`
  rather than guessed at.
- The behavioral-verb list is a deliberately small, curated set, not an
  exhaustive NLP model -- prose without one of these verbs is still
  evaluated (via a higher content-word threshold), never silently
  rejected outright.
- Intent contradiction detection is not implemented (see above).
- Linked-issue and commit-message intent sources are deferred (see
  above).
- Missing-surface candidates are heuristic evidence, not proof -- they
  must survive the existing reviewer/critic pipeline like any other
  finding before ever reaching GitHub.
- Says nothing about *why* a surface wasn't updated (a deliberate
  decision, or an oversight) -- that judgment is left to the existing
  reviewer/critic, never fabricated here.
