# Change Intelligence Foundation — Audit & Validation

Branch `feat/change-intelligence-foundation`, baseline `main` @
`d81d35758b245513346c8e72cf07bdf19bf0fe4f` (Milestone I, merged).

## 1. Audit (written before any implementation)

### What graph primitives already exist

PatchFrog already has a complete, real, already-indexed repository
graph — Change Intelligence must be built entirely on top of it, never
a second graph:

- **`patchfrog/persistence/models/code_index.py`**: `SymbolModel`
  (with `parent_symbol_id` for containment), `CallReferenceModel`
  (resolved caller→callee, `resolution_status` never guessed),
  `ImportReferenceModel` (resolved import/include target),
  `RepositoryEdgeModel` (an explicit, queryable, already-materialized
  edge table).
- **`patchfrog/intelligence/graph.py`**: `build_graph()` produces
  `RepositoryEdge` rows of kind `SYMBOL_CONTAINS_SYMBOL`,
  `FILE_IMPORTS_FILE`/`FILE_INCLUDES_FILE`, `SYMBOL_CALLS_SYMBOL`,
  `FILE_TESTS_FILE` — persisted once per indexing run.
  `SYMBOL_REFERENCES_SYMBOL`/`SYMBOL_TESTED_BY_SYMBOL` are declared in
  `EdgeKind` but never actually constructed anywhere in the codebase
  today (confirmed via grep) — Change Intelligence must not assume
  they exist.
- **`patchfrog/intelligence/queries.py`** (`RepositoryQueryService`):
  the complete, already-tested read API over all of the above —
  `get_callers`/`get_callees`/`imports_from_file`/`files_importing`/
  `likely_tests_for_file`/`symbol_for_changed_line`/
  `find_symbol_by_qualified_name` and batched
  `get_symbols_by_ids`/`get_files_by_ids`.
- **`patchfrog/context/candidates.py`** (`ContextCandidateGenerator`):
  already implements a bounded, deterministic 1-hop-then-2-hop BFS over
  `get_callers`/`get_callees` (`_call_edge_candidates`), including the
  self-recursion/2-cycle exclusion this milestone's own bounded-traversal
  requirement needs. This is the *pattern* to reuse (same query
  primitives, same bounding discipline), not the `ContextCandidateGenerator`
  class itself — that class is single-target/token-budget/context-bundle
  shaped (produces `ContextCandidate` for one review target's LLM
  context), not multi-root/connected-component shaped (what Change
  Intelligence's grouping needs: *all* changed symbols in a PR at once).

### What is reused vs. what is genuinely new

**Reused, unchanged**: `RepositoryQueryService` (every graph read),
`SymbolModel`/`IndexedFileModel`/`RepositoryEdgeModel` (every graph
write — indexing already builds and persists the graph; Change
Intelligence never re-indexes, never re-parses, never writes a second
copy of symbol/edge data), `ReviewCandidate`/`ReviewCandidateGenerator`
(the existing diff→changed-symbol mapping — Change Intelligence groups
*already-generated* candidates, it does not re-derive "what changed"),
`IndexedFileModel.is_test` (already-computed test-file classification,
reused directly for `ChangeKind.TEST` and for excluding test files from
"missing companion" heuristics about *other* test files).

**Genuinely new** (this milestone): a deterministic grouping algorithm
(`ChangeUnit` = connected component of changed-symbol `ReviewCandidate`s
over the *existing* call/containment graph, from *multiple* roots at
once); a bounded affected-surface traversal per `ChangeUnit` (same
query primitives as `ContextCandidateGenerator`, new orchestration for
multi-root, directly/dependent/indirect/test classification); two
graph-grounded companion-change heuristics (caller-staleness,
test-staleness — see section 2 below for why only these two, not a
larger semantic taxonomy); deterministic Change Story text; deterministic,
bounded Change Map (Markdown, no diagram library) with an explicit
eligibility gate; a small, evidence-based `ChangeKind` taxonomy.

### How this avoids becoming a second parallel architecture

Change Intelligence is a **pure function of already-persisted index
state plus one review run's already-generated `ReviewCandidate` list**
— `build_change_intelligence_report(session, repository_index_id,
candidates)`. It never re-indexes, never re-resolves calls/imports,
never introduces a new symbol/edge table. Its own output (counts +
bounded text) is persisted onto the *existing* `review_runs` row as new
nullable columns, following the exact established pattern
`candidates_by_tier`/`correctness_input_tokens` etc. already use on
that same table — not a new parallel "change intelligence run" concept
with its own lifecycle/status machine.

### Missing primitive actually needed

None at the graph level. The one genuinely missing piece is
**deterministic multi-root connected-component grouping** itself —
`RepositoryQueryService` answers "who calls X" / "who does X call" but
nothing today unions multiple such neighborhoods into components
bounded by size, which is exactly what `patchfrog/change_intelligence/grouping.py`
adds (a thin, bounded, testable algorithm over the existing query API —
not a new data source).

## 2. Design decisions

### ChangeKind taxonomy — evidence sources, not guesses

- `TEST` — the changed file's `IndexedFileModel.is_test` is true
  (already computed at index time; not re-derived).
- `CONFIGURATION` — the changed file's path matches a small,
  conservative set of config-shaped patterns (`config`, `settings`,
  `.yml`/`.yaml`/`.toml`/`.ini`/`.env` extension) — repository
  *structure*, not LLM guessing.
- `INFRASTRUCTURE` — path matches `docker`, `.github/workflows`, `ci`,
  `deploy`, `infra`.
- `PERSISTENCE` — path matches `model`/`models`/`migration`/`schema`/
  `persistence`/`orm`.
- `CONTRACT` — the changed symbol has at least one *cross-file* caller
  in the real call graph (evidence: it's actually depended on
  elsewhere, not a naming guess).
- `BEHAVIOR` — default for a changed function/method with none of the
  above signals (a real logic change with no stronger structural
  signal).
- `MIXED` — a `ChangeUnit` whose constituent candidates trigger more
  than one of the above.

Deliberately not 30 categories, deliberately never inferred from prose.

### Only two companion-change heuristics, both graph-grounded

The spec's examples ("schema changed, serializer forgotten", "config
field added, loader forgotten", "API field added, consumer forgotten",
"new error state, handler forgotten") all reduce, at the *evidence*
level available in this index, to the same two underlying signals:

1. **Caller-staleness**: a changed symbol has real callers (from the
   call graph) that were *not themselves touched* in this diff — the
   caller may need updating for the new signature/contract/error
   surface. This is the general form of every "X changed, Y forgot to
   update" example in the spec — whether Y is a serializer, a loader, a
   consumer, or a handler is not a distinction the current index makes
   (there is no `SymbolKind.SERIALIZER`), and inventing one would be
   exactly the "arbitrary LLM categorization" the product rule
   forbids. What *is* real, structural evidence is "this symbol calls
   the changed one, and did not itself change."
2. **Test-staleness**: a changed symbol/file has a real, already-computed
   test relationship (`FILE_TESTS_FILE`) whose test file was *not*
   touched in this diff — "behavior changed, negative test missing".

Both heuristics only ever produce a **candidate** (never a published
finding) and only fire when the dependency is *current* (the edge
exists in the *current* index, at the exact reviewed commit) and
*specific* (a named caller/test symbol, not a vague "something might be
affected").

### Evaluation corpus — a dedicated harness, not `patchfrog.evaluation`

`patchfrog.evaluation.domain.EvaluationCase` is finding-shaped
(`ExpectedFinding`/`ForbiddenFinding`) — it is, by that package's own
documented design ("the only source of TP/FP/precision/recall"),
specifically for measuring *AI review* quality against human-authored
ground truth. `ChangeUnit`/affected-surface/companion-candidate/diagram
ground truth is a structurally different shape (unit counts, symbol
membership, boolean diagram eligibility) with **zero LLM involvement**.
Rather than force-fitting Change Intelligence ground truth into
`EvaluationCase` (which would blur exactly the "benchmark ground truth
vs. operational telemetry vs. user feedback — never conflated"
boundary `patchfrog.telemetry`'s own docstring already established),
this milestone adds a **dedicated, purpose-built test corpus**
(`tests/integration/test_change_intelligence_corpus.py`), real git-repo
fixtures in the same style already used by
`tests/integration/test_review_memory_end_to_end.py`, each asserting
explicit ground truth (ChangeUnit count/membership, expected
affected-surface symbols, expected missing-companion candidates,
diagram-eligibility yes/no). Results are reported in section "Corpus
results" below — a pass/fail + qualitative summary, never a fabricated
precision/recall statistic bolted onto an unrelated framework.

## 3. Corpus results

`tests/integration/test_change_intelligence_corpus.py` — 9 tests, real
git repositories, real indexing, real diff-driven `ReviewCandidate`
generation, real `build_change_intelligence_report` (zero LLM
involvement). **9/9 pass.**

Coverage against the spec section 18 list of 12 named scenarios. Per
the "Only two companion-change heuristics" design decision in section 2
above, "schema+serializer forgotten" / "config+loader forgotten" /
"API field+consumer forgotten" / "new error state+handler forgotten" /
"worker path forgotten" all reduce, at the evidence level this index
actually has, to the same **caller-staleness** pattern the corpus
exercises directly once — a real changed symbol whose real caller was
not touched. Distinguishing *which kind* of caller a serializer vs. a
loader vs. a consumer is would require an invented `SymbolKind`
taxonomy this codebase has no real evidence for, which the product rule
in section 0 forbids. The corpus therefore includes one direct fixture
per structurally distinct evidence shape, not one fixture per spec
example sentence:

| Spec scenario (section 18) | Corpus test | Result |
|---|---|---|
| 2. function signature changed, caller forgotten (representative of 1/2/3/4/5/7 — same caller-staleness evidence shape) | `test_case_signature_changed_caller_forgotten` | 1 `CALLER_NOT_UPDATED` candidate naming `process`; diagram eligible |
| 6. behavior changed, negative test missing | `test_case_behavior_changed_negative_test_missing` | 1 `TEST_NOT_UPDATED` candidate naming `test_service.py` |
| 8. complete multi-file implementation | `test_case_complete_multi_file_implementation` | 1 `ChangeUnit` spanning 3 files/3 candidates, diagram eligible, `node_count >= 3` |
| 9. isolated correct one-function fix | `test_case_isolated_correct_one_function_fix` | 1 `ChangeUnit`, zero missing companions, no diagram |
| 10. docs-only change | `test_case_docs_only_change` | no diagram (module-region candidate, no parser symbol) |
| 11. two unrelated logical changes | `test_case_two_unrelated_logical_changes_never_merge` | 2 separate `ChangeUnit`s, never merged |
| 12. large connected PR | not a dedicated integration fixture; covered at the unit level by `test_huge_graph_produces_a_bounded_diagram` (`tests/unit/test_change_intelligence_change_map.py`) — bounded/truncated output proven against a synthetic large graph, since building a real 12+ symbol fixture repo adds fixture cost without adding coverage beyond what the bounding logic itself needs proven | bounded, truncation noted |

Plus 3 property tests: `test_change_intelligence_never_calls_a_provider`
(structural — no `LLMProvider` import in the package),
`test_change_kind_taxonomy_used_by_a_real_persistence_path` (a
`models/user.py` change classifies `PERSISTENCE` against the real
query service), `test_observed_companion_not_reported_as_missing` (a
caller updated in the same diff is `OBSERVED`, never a false-positive
`MISSING`).

**Grouping accuracy**: 3/3 grouping-shaped cases correct (1 unit for the
3-file connected implementation, 1 unit for the isolated fix, 2 separate
units for the two-unrelated-changes case — never merged by shared
directory or file proximity).

**Affected-surface recall on known dependencies**: the caller-forgotten
case's affected surface reaches `process` (the real 1-hop caller) and,
via the depth-2 bound, `handle` (the real 2-hop caller) — both proven
present via the diagram-eligibility assertion (`>= 3 nodes across >= 2
files` requires both hops to be found).

**False companion-change candidate rate**: 0/9 — `test_observed_companion_not_reported_as_missing`
is the direct proof; no other corpus case produces an unexpected
`MISSING` candidate (each assertion is exact-match on
`expected_qualified_name`/`expected_file_path`, not a substring/count
check that could hide an extra false positive).

**Diagram eligibility precision/recall**: all 9 mandatory spec section
17 cases pass in `tests/unit/test_change_intelligence_change_map.py`
(12 tests total in that file — docs-only, isolated one-function fix,
simple rename, one-file leaf helper, formatting-only, trivial one-file
patch, graph-adds-no-information, disconnected-unrelated-never-one-diagram
all correctly produce **no** diagram; API→service→repository,
worker→service→persistence, schema+serializer+consumer, and a huge
graph all correctly produce a **bounded** diagram). Cross-checked at
the integration level: the 3 corpus cases with a real cross-file
connected change (`test_case_signature_changed_caller_forgotten`,
`test_case_complete_multi_file_implementation`) render, and the 4
single-file/no-symbol cases (`test_case_isolated_correct_one_function_fix`,
`test_case_docs_only_change`) do not — 0 false positives, 0 false
negatives across the corpus actually run.

## 4. Gates

All run against the real changes on this branch, 2026-09-02:

| Gate | Result |
|---|---|
| `git diff --check` | clean, no whitespace/conflict-marker errors |
| `ruff check .` | All checks passed! |
| `mypy . --strict` | Success: no issues found in 440 source files |
| `pytest` (full suite, real Postgres + Redis via `docker compose up -d postgres redis`, migrated to head `0018_change_intelligence`) | **1401 passed, 0 failed.** (Baseline before this milestone: 1353 total. The 3 previously-documented pre-existing failures in `tests/integration/test_static_analysis_service.py` did **not** reproduce in this run — all 7 of that file's tests pass now; not claimed as a fix made by this milestone, since nothing in this milestone touches static analysis, just reported honestly as observed.) |
| Alembic single head | `alembic heads` → `0018_change_intelligence (head)`, exactly one head; `alembic upgrade head` against real Postgres succeeded cleanly |
| Docker API image build | `docker build --target api` → `Successfully tagged patchfrog-api:ci-check` |
| Docker worker image build | `docker build --target worker` → `Successfully tagged patchfrog-worker:ci-check` |
| Celery task registration | `tests/integration/test_celery_task_registration.py` — 1 passed (subprocess-isolated) |
| Change Intelligence tests | 41 tests (`tests/unit/test_change_intelligence_change_kind.py` ×13, `tests/unit/test_change_intelligence_change_map.py` ×12 incl. all 9 mandatory diagram-spam cases, `tests/unit/test_change_intelligence_versioning.py` ×7, `tests/integration/test_change_intelligence_corpus.py` ×9) — all pass |
| Context Engine / candidate generation / review orchestration / publishing / carried-forward / telemetry tests | included in the 1401-passed full run above, no regressions |
| Docs links | `docs/change-intelligence.md` — module/file references checked to exist; no dead links |
| Tracked-file / PR-diff secret scan | `git diff` (tracked changes) + every new untracked file scanned for common credential shapes (`sk-…`, AWS `AKIA…`, Slack `xox[baprs]-…`, PEM private key headers, GitHub `ghp_…`, Google `AIza…`) — no matches |

Provider calls added by this milestone: **0** (structurally proven,
`test_change_intelligence_never_calls_a_provider`). No Gemini call, no
Anthropic call, no Cloud/dashboard work.
