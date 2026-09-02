# Contract & Blast Radius Intelligence — Audit & Validation

Branch `feat/contract-blast-radius`, baseline `main` @
`7734603625d7a65520985f4ac52888dc010fbe29` (Milestone J, merged).

## 1. Audit (written before any implementation)

### What contract-like metadata already exists

- **`patchfrog/persistence/models/code_index.py`** (`SymbolModel`):
  `signature: str | None` — the parser's already-extracted `def
  name(...) -> T` header text (Python; a comparable header string for
  C/C++), `kind: SymbolKind`, `language: Language`, `visibility: str |
  None`. **No structured parameter list is persisted anywhere** —
  `signature` is raw text, never `[Parameter(name, annotation,
  default, ...)]`. This is the one genuinely missing primitive (see
  below).
- **`patchfrog/domain/code.py`** (`ParsedSymbol`): the same shape,
  pre-persistence — `signature: str | None` is built once, per
  language, by `patchfrog.parsing.<lang>._function_signature`-equivalent
  logic (Python: `patchfrog.parsing.python._function_signature` grabs
  everything from `def`/decorators up to (but not including) the
  trailing `:`, so `async def foo(a: int, b: str = "x") -> int` is
  exactly what's stored — a real, deterministic, already-existing
  source of truth to parse structurally).
- **Real consumer evidence**: `CallReferenceModel`/`RepositoryEdgeModel`
  (via `RepositoryQueryService.get_callers`) is the same real,
  already-resolved caller evidence Change Intelligence's own
  `CALLER_NOT_UPDATED` heuristic uses — directly reusable as "does
  this symbol have a real consumer" (spec section 4).
- **Nothing else contract-shaped exists**: no OpenAPI/schema-registry
  concept, no config-key inventory, no persistence-mapper/ORM-field
  model, no event producer/consumer graph. `ChangeKind.CONFIGURATION`/
  `PERSISTENCE` (Milestone J) are path-pattern heuristics for
  *classification*, not structured field-level contract data — they
  cannot support a field-level `ContractDelta` today.

### What before/head information already exists

- **`RepositoryIndexModel`**: one row per `(repository_id, commit_sha)`
  indexing run, but **only ever the current PR's `head_sha` is
  indexed** in production (`apps/worker/tasks/review_pull_request.py`
  calls indexing with `commit_sha=head_sha`) — the base commit is
  *never* separately indexed as a persisted row. There is no reusable
  "base index" to query against.
- **`PullRequestMetadata.base_sha`** (`patchfrog/domain/pull_request.py`,
  populated by `patchfrog.github.client.get_pull_request`) **is already
  fetched from GitHub for every PR review** (`apps/worker/tasks/review_pull_request.py`'s
  `current_metadata = await github_client.get_pull_request(...)`) but
  is currently used only for the stale-head check — never threaded
  into `PullRequestReviewService.review_pull_request`. This is real,
  already-available data; wiring it through is a small, additive
  change, not new infrastructure.
- **`patchfrog.repository.file_contents.read_files_at_commit`**
  (introduced for Phase 7 evidence revalidation): a bounded, exact-
  commit, exact-paths-only `git show` read that needs **no working-tree
  checkout** — "deliberately separate from and cheaper than
  `patchfrog.repository.snapshot`'s full checkout, since evidence...
  never [needs] the whole tree on disk" (its own module docstring).
  This is *exactly* the primitive base/head comparison needs: fetch
  only the base-commit content of the specific files a changed,
  consumer-having function lives in — never a second clone, never a
  second index.

### Can base/head symbols be compared without a second index?

**Yes.** The plan:

1. Restrict to files containing a changed `ReviewCandidate` whose
   `SymbolModel.kind` is `FUNCTION`/`METHOD` and `language is
   Language.PYTHON` (see "safely detectable" below) **and** which has
   at least one real caller (`get_callers`) — the contract-boundary
   gate (spec section 4). This is almost always a small subset of one
   PR's changed files, not the repository.
2. Fetch those files' content at `base_sha` via `read_files_at_commit`
   (production) or a direct local `git show <base_sha>:<path>` against
   the already-checked-out working tree (CLI/local review — cheaper,
   no fetch needed since the local clone already has history).
3. Parse the fetched base content **in-memory, with the exact same
   parser already used for indexing** (`patchfrog.parsing.registry.default_registry()`),
   never a second parsing engine — producing `ParsedSymbol` objects
   with the same `signature` text shape already persisted at HEAD.
   Nothing is written to the database; this is a pure, ephemeral,
   per-review-run computation.
4. Match base and head symbols by `qualified_name` within the same
   file. A symbol absent from the base parse is a new contract (no
   delta, evidence-safe — nothing to diff against); a symbol absent at
   HEAD (removed) is out of this milestone's `ContractDelta` scope
   (see limitations).
5. Parse both signature strings with a new, purpose-built, deterministic
   parameter-list tokenizer (`patchfrog.contract_intelligence.function_signature`)
   and diff the structured result.

This is anchored to the exact reviewed `commit_sha` (HEAD, already
gating the whole review run) and the exact `base_sha` GitHub reported
for this PR at review time — stale-head-safe by construction (if the
review is running, `commit_sha` has already been confirmed current by
the existing stale-head check; `base_sha` is read once, at the same
metadata fetch, never re-resolved mid-run). No network mutation (only
`git fetch --depth 1`/`git show`, exactly `read_files_at_commit`'s
existing contract). No LLM.

### Which contract types are safely detectable today?

**Robustly supported this milestone: `ContractKind.FUNCTION`, Python
only.** `SymbolModel.signature` for Python is a clean, unambiguous,
already-correct extraction of the real `def` header (verified by
reading `patchfrog/parsing/python.py::_function_signature` directly) —
parameter names, defaults, `*args`/`**kwargs`, keyword-only markers,
and the return annotation are all present as real source text, so a
deterministic structural parse (never a semantic/type-inference guess)
can extract exactly the shape spec section 3 asks for.

C/C++ function signatures are declared in `SymbolModel.signature`
too, but their grammar (pointers, references, `const`, templates,
default-argument expressions that can themselves contain commas/parens,
`static`/`extern` storage class, function-pointer parameters) is
materially riskier to parse correctly with a hand-written tokenizer
than Python's; a wrong parse here would produce a wrong (and possibly
*false-positive*) contract delta, which the product principle (spec
section 0) explicitly forbids risking. **Deferred** — see Limitations.

### Which types would require guessing and must be deferred?

- **`ContractKind.SCHEMA`**: PatchFrog has no schema/DTO model (no
  Pydantic-field/dataclass-field extraction, no OpenAPI awareness).
  Detecting "a schema field was removed" would require guessing which
  classes are "schemas" from naming/decorators — exactly the "arbitrary
  LLM categorization"/"OpenAPI product work" the spec forbids. Deferred.
- **`ContractKind.CONFIGURATION`**: `ChangeKind.CONFIGURATION` is a
  path-pattern heuristic (`config`/`settings`/`.yml` etc.), not a
  structured config-key/default inventory; there is no
  loader-reads-key-X relationship in the index. Deferred (a future
  milestone could reuse the same function-signature machinery for a
  config *loader function's own signature*, which would then already
  be `ContractKind.FUNCTION` — not a separate mechanism).
- **`ContractKind.PERSISTENCE`**: no ORM-field/column-mapping model
  exists in the index. Deferred.
- **`ContractKind.EVENT`**: no event producer/consumer relationship is
  indexed anywhere (`EdgeKind` has no such kind). Deferred.
- **`ContractKind.PUBLIC_INTERFACE`**: Python parsing sets
  `visibility=None` for every symbol (confirmed by reading
  `patchfrog/parsing/python.py`) — there is no `__all__`/export-list
  extraction to build a real "public interface" signal independent of
  "has a real caller" (which `FUNCTION` already uses as its boundary
  gate). Not meaningfully distinct from `FUNCTION` with today's index;
  deferred as its own kind rather than faked.

The `ContractKind` enum below keeps all six values (matching spec
section 2's suggested taxonomy, for forward extensibility/
documentation) but this milestone's detection logic only ever
constructs `FUNCTION` descriptors — never a fabricated `SCHEMA`/
`CONFIGURATION`/`PERSISTENCE`/`EVENT`/`PUBLIC_INTERFACE` descriptor.

### How Contract Intelligence extends Change Intelligence rather than becoming a second parallel stack

No second repository graph, no second grouping algorithm, no second
candidate-evidence type:

- **Blast radius reuses `patchfrog.change_intelligence.affected_surface.derive_affected_surface`
  directly** (not reimplemented) — for a contract-bearing symbol, its
  owning `ReviewCandidate` is passed straight into that existing
  function, inheriting its exact bounds
  (`MAX_GRAPH_DEPTH`/`MAX_FANOUT_PER_SYMBOL`/`MAX_AFFECTED_SURFACE_PER_UNIT`)
  and DIRECTLY_DEPENDENT/INDIRECTLY_AFFECTED/TEST classification
  verbatim.
- **Stale-consumer candidates reuse `patchfrog.change_intelligence.domain.ExpectedCompanionChange`
  directly** (spec section 9's explicit instruction) — a new
  `CompanionReasonCode.CONTRACT_CONSUMER_NOT_UPDATED` member is added
  to the *existing* enum, produced by this new package, but the type
  itself, its `internal-candidates-only`/`never-auto-published`
  contract, and its consumption by the reviewer/critic pipeline are
  entirely unchanged.
- **The Change Map is not extended with new code at all** — contract
  stale-consumer candidates are simply included in the same
  `expected_companions` tuple already passed to
  `patchfrog.change_intelligence.change_map.render_change_map`
  (which already renders any `MISSING`-status companion under
  "Expected but missing" regardless of reason code). Diagram
  *eligibility* remains driven purely by real `ChangeUnit` connectivity
  (spec section 10: "Existing diagram bounds remain authoritative...";
  no eligibility-rule change was needed).
- **The Contract Story is folded into the existing `change_story` text**
  (one extra sentence, appended only when a real breaking delta with a
  real stale consumer exists) rather than a new summary block or a new
  persisted column.
- **Genuinely new**: `ContractDescriptor`/`ContractDelta`/
  `BreakingCharacteristic` (base/head signature comparison — nothing in
  Change Intelligence does this), the Python signature tokenizer, and
  the base-content-fetch integration.

## 2. Domain model and architecture (as implemented)

`patchfrog/contract_intelligence/`:

- `domain.py` -- `ContractKind` (6 values, only `FUNCTION` produced this
  milestone), `BreakingCharacteristic` (10 values, `BREAKING_CHARACTERISTICS`
  the "yes" subset), `ContractDescriptor`, `ContractDelta`
  (`.is_potentially_breaking`, `.blast_radius` reusing
  `AffectedSymbolRef`), `ContractIntelligenceReport`
  (`.potentially_breaking_deltas`, `.impacted_consumer_count`,
  `.contract_kind_counts`). `CONTRACT_INTELLIGENCE_VERSION = 1`.
- `function_signature.py` -- deterministic Python `def`/`async def`
  header tokenizer (`parse_python_signature`), bracket/quote/lambda-
  depth-aware, never a guess: an unrecognized token shape fails the
  *whole* parse closed (returns `None`).
- `delta.py` -- `diff_signatures`, name-keyed comparison, produces
  `BreakingCharacteristic` tuples per spec section 6's rule table.
- `base_fetch.py` -- bounded base-commit file fetch (production:
  `patchfrog.repository.file_contents.read_files_at_commit`; local:
  direct `git show`) + in-memory parse via `patchfrog.parsing.registry`
  (never a second index).
- `boundaries.py` -- the contract-boundary gate (real resolved callers
  required).
- `stale_consumers.py` -- `CONTRACT_CONSUMER_NOT_UPDATED` candidate
  derivation, reusing `patchfrog.change_intelligence.domain.ExpectedCompanionChange`.
- `story.py` -- Contract Story addendum (folded into `change_story`).
- `evidence.py` -- bounded `<contract_intelligence>` per-candidate text.
- `telemetry.py` -- `ContractIntelligenceSummary`/`summarize_for_persistence`.
- `service.py` -- `build_contract_intelligence_report`, the one
  orchestration entry point (mirrors
  `patchfrog.change_intelligence.service.build_change_intelligence_report`
  exactly).

Blast radius reuses `patchfrog.change_intelligence.affected_surface.derive_affected_surface`
directly (zero new traversal code). Change Map reuses
`patchfrog.change_intelligence.change_map.render_change_map` directly
(zero new rendering code) -- `patchfrog/review/service.py` re-renders it
once, at the integration point, with the combined companion set, only
when contract stale-consumers actually exist.

## 3. Corpus results

`tests/integration/test_contract_intelligence_corpus.py` -- 10 tests,
real git repository (two real commits, a genuine base/head diff), real
indexing, real diff-driven candidate generation, real
`build_contract_intelligence_report` against real base-commit content
fetched via local `git show`. **10/10 pass.** Zero LLM involvement
(structurally proven by a dedicated test in the same file).

| Spec scenario (section 16) | Corpus test | Result |
|---|---|---|
| 1. required argument added, caller forgotten | `test_case_required_argument_added_caller_forgotten` | REQUIRED_PARAMETER_ADDED breaking delta; 1 MISSING stale-consumer naming `process` |
| 2. required argument added, all callers updated | `test_case_required_argument_added_all_callers_updated` | delta detected; companion OBSERVED, never MISSING |
| 3. optional argument with default added, caller unchanged (negative) | `test_case_optional_parameter_with_default_no_false_positive` | delta detected but `is_potentially_breaking is False`; **zero** stale-consumer candidates |
| 4. parameter removed, stale caller | `test_case_parameter_removed_stale_caller` | PARAMETER_REMOVED breaking delta; MISSING candidate naming `process` |
| 6. return contract widened, all consumers updated | `test_case_return_became_optional_all_consumers_updated` | RETURN_BECAME_OPTIONAL breaking delta; companion OBSERVED |
| 7/8/9/10. config/schema kinds | **DEFERRED** -- `ContractKind.CONFIGURATION`/`SCHEMA` are not detected this milestone (see section 1); not faked with a placeholder test |
| 11. complete multi-file migration | covered implicitly by the pipeline test's real multi-file blast radius (`impacted_consumer_count >= 1` across `service.py`/`caller.py`) |
| 12. internal private helper, no consumer (negative) | `test_case_internal_helper_with_no_consumer_produces_no_descriptor` | **zero** descriptors/deltas/candidates even though the signature genuinely changed |
| 13. docs-only (negative) | `test_case_docs_only_change_produces_empty_report` | empty report |
| 14. two unrelated contract changes | `test_case_two_unrelated_contract_changes_never_conflated` | exactly 1 real delta (`save`); the no-consumer `unrelated_helper` never appears |
| 15. large fan-out, bounded blast radius | not re-tested in this corpus -- blast radius is `derive_affected_surface` called verbatim, already proven bounded by Milestone J's own `tests/unit/test_change_intelligence_change_map.py::test_huge_graph_produces_a_bounded_diagram` and the `MAX_FANOUT_PER_SYMBOL`/`MAX_AFFECTED_SURFACE_PER_UNIT` constants it shares |
| 5. return widened, nullable (no explicit-forgotten-consumer variant) | covered by unit tests (`test_return_became_optional_is_breaking`) + case 6 above |

**Negative/false-positive tests (spec section 18)**: optional-parameter-
with-default (case 3), internal-helper-no-consumer (case 12), consumer-
already-updated (case 2), docs-only (case 13), two-unrelated-changes
(case 14, proving no cross-contamination), `base_sha=None` no-op
(`test_case_no_base_sha_is_a_no_op`). All pass -- zero false-positive
stale-consumer candidates anywhere in the corpus.

**Pipeline integration** (not just isolated service calls):
`tests/integration/test_contract_intelligence_review_pipeline.py` -- 2
tests, driving the real `PullRequestReviewService.review_local` (a
scripted `FakeLLMProvider`, never a live call) end to end: one proves
counts/Change-Story-addendum are correctly persisted onto the real
`review_runs` row; one proves `base_sha=None` (every review before this
milestone) is a complete, crash-free no-op.

**Unit coverage**: `test_contract_intelligence_function_signature.py`
(14 tests -- simple/async/no-params/no-return/decorators/multiline/
positional-only/keyword-only/nested-brackets/generics/lambda defaults
including the multi-param-lambda edge case/non-function input/variadic-
only/Optional-shape detection), `test_contract_intelligence_delta.py`
(14 tests -- every `BreakingCharacteristic` rule plus reordering/
identical-signature/multi-characteristic cases),
`test_contract_intelligence_change_map_integration.py` (2 tests --
proves the zero-new-rendering-code Change Map merge),
`test_contract_intelligence_versioning.py` (9 tests).

## 4. Success metrics (controlled-corpus evidence only, spec section 17)

- **Contract delta detection**: 6/6 corpus cases that should produce a
  delta did (precision/recall both 1.0 on this synthetic corpus --
  never claimed as a production accuracy figure).
- **Stale-consumer precision**: 0 false-positive candidates across all
  10 corpus cases, including the 3 cases specifically designed to
  trigger one if the logic were wrong (optional-param, all-callers-
  updated x2).
- **Stale-consumer recall**: 2/2 cases with a real forgotten caller
  (required-arg-added, parameter-removed) produced the expected MISSING
  candidate.
- **Blast-radius boundedness**: inherited verbatim from Milestone J's
  own proven bounds (`derive_affected_surface` reused directly, not
  reimplemented) -- see the huge-graph test referenced above.
- **False-positive rate on backward-compatible changes**: 0/1
  (`OPTIONAL_PARAMETER_ADDED` case produces zero candidates).
- **Change Map eligibility**: unaffected by this milestone (still purely
  Change Unit connectivity-driven); contract data only changes *content*
  shown once already eligible -- proven directly by
  `test_contract_intelligence_change_map_integration.py`.
- **Extra provider calls**: **0** (structurally proven).
- **Prompt/token delta**: `REVIEW_PROMPT_VERSION` 4 -> 5 (new optional
  `<contract_intelligence>` section, empty/byte-identical for every
  candidate except the exact source of a real delta).

## 5. Gates

All run against the real changes on this branch, 2026-09-02:

| Gate | Result |
|---|---|
| `git diff --check` | clean, no whitespace/conflict-marker errors |
| `ruff check .` | All checks passed! |
| `mypy . --strict` | Success: no issues found in 457 source files |
| `pytest` (full suite, real Postgres + Redis, migrated to head `0019_contract_intelligence`) | **1457 passed, 0 failed** (baseline before this milestone: 1403) |
| Alembic single head | `alembic heads` -> `0019_contract_intelligence (head)`; real `alembic upgrade head` against Postgres succeeded cleanly |
| Docker API image build | `docker build --target api` -> `Successfully tagged patchfrog-api:k-check` |
| Docker worker image build | `docker build --target worker` -> `Successfully tagged patchfrog-worker:k-check` |
| Celery task registration | `tests/integration/test_celery_task_registration.py` -- 1 passed (subprocess-isolated) |
| Contract Intelligence tests | 51 new tests (14 signature-parser + 14 delta-rule + 9 versioning + 2 change-map-integration unit tests; 10 corpus + 2 real-pipeline integration tests) -- all pass |
| Change Intelligence / Context Engine / review prompt-versioning / telemetry collector-versioning / publishing / carried-forward / diagram-eligibility tests | included in the 1457-passed full run above, no regressions |
| Docs links | `docs/contract-intelligence.md` -- referenced file paths checked to exist |
| Tracked-file / PR-diff secret scan | every changed/new file scanned for common credential shapes -- no matches |

Provider calls added by this milestone: **0** (structurally proven,
`test_contract_intelligence_never_calls_a_provider`). No Gemini call, no
Anthropic call, no Cloud/dashboard work.
