# M6 (Upstream Change Detection + Consumer Impact / Blast Radius) + M7 (Migration Planner + Generated Fix)

Status: plan written before code (workflow step 2); implementation and
validation results are appended in the RESULT section below.

Baseline: `main` @ `c66c562` (M4 + M5 squash merge). Branch:
`feat/m6-m7-upstream-change-migration`.

Product direction:

> PatchFrog keeps software compatible with the APIs and SDKs it depends on.

upstream API/SDK change -> contract diff -> breaking/risky classification
-> affected consumer discovery -> blast radius -> migration plan ->
generated patch.

Invariant: **change -> impact -> evidence -> verification -> decision.**
In this milestone "verification" means deterministic structural/contract
validation only (patch safety gates). Executable/runtime verification is
M8 and is **not** started here. Automated migration PRs are M9 and are
**not** started here.

## 0. Phase 0 audit -- what M5 already gives us (reused, not duplicated)

| Need | Existing primitive (reused as-is) |
|---|---|
| dependency identity, usage sites, versions | `patchfrog/dependencies/domain.py` (`ExternalDependency`, `DependencyUsageSite`, `DependencyVersion`) |
| usage sites with enclosing symbols | `discover_dependencies` (Python symbols via the same tree-sitter parser as the index; JS/TS lexical spans) |
| SDK call-chain tokens | `scan.py`: `SDK_CALL` token = chain after the client (`checkout.sessions.create`) |
| HTTP endpoint tokens | `HTTP_ENDPOINT` (`host` + sanitized path, placeholders -> `{}`), `OPENAPI_PATH_REFERENCE` |
| normalized OpenAPI contract | `openapi.normalize_spec` already keeps params (name/in/required/schema), request bodies, responses, security, security schemes, component property shapes -- exactly what a diff needs; `path_pattern` for path matching |
| contract history | `external_contract_snapshots` (`normalized_contract` JSON per fingerprint) -- the "old" side of a registry-backed diff |
| per-repo registry | `external_dependencies` + `external_dependency_usage_sites` -- the cross-repo consumer set |
| code graph | `intelligence.graph.build_graph` over `parsing.registry.default_registry` + `RepositoryResolver` + `infer_test_relationships` (pure, DB-free) |
| provider abstraction | `review.provider.LLMProvider` + `review.providers.fake.FakeLLMProvider` |
| secret-store guard | `dependencies.files.is_secret_store_path` |

Gaps found:

- no contract-diff engine, no change-event model, no classifier;
- no consumer mapper beyond "all usage sites of a dependency";
- the parser registry covers Python/C/C++ only -> JS/TS gets usage sites
  (lexical) but no caller graph; blast radius will say so explicitly;
- no patch-generation primitive anywhere (Fix Verification works on
  commits after the fact -- M8/M9 territory); M7 adds the smallest one;
- Cross-Repo Intelligence (R) is operator-registered repo->repo PR
  contract relations; it has no notion of external dependencies. The
  trusted evidence for "which repositories consume dependency X" is the
  M5 registry itself (each row came from real discovery evidence), so
  M6.7 is registry-backed + local multi-checkout, never name-inferred.

## 1. Plan

New packages (no parallel state with M5):

- `patchfrog/upstream/` (M6) -- `UPSTREAM_CHANGE_VERSION = 1`
  - `domain.py` -- `ExternalChangeEvent`, `ExternalChangeSource`,
    `ExternalChangeKind`, `ExternalContractRevision`, `DependencyRelease`,
    `DependencyTarget`, `ContractDiffItem` (+ `DiffItemKind`,
    `DiffSubject`), `CompatibilityClass`
    (NON_BREAKING/POTENTIALLY_BREAKING/BREAKING/UNKNOWN), `ChangeRisk`
    (SAFE/LOW_RISK/REVIEW_REQUIRED/BREAKING), `ChangeClassification`.
  - `openapi_diff.py` -- deterministic diff over M5-normalized contracts
    (endpoints, methods, parameters, request bodies, responses, auth,
    components). Consumer-direction aware: a schema used only in
    responses and one used only in requests classify differently.
  - `sdk_surface.py` -- a small provider-agnostic SDK surface document
    (`patchfrog_sdk_surface: 1`: symbols + params + modules) and its diff.
  - `package_version.py` -- semver-ish comparison; a version change alone
    never proves breakage (major -> POTENTIALLY_BREAKING).
  - `hints.py` -- explicit change hints (`patchfrog_change_hints: 1`):
    symbol/parameter/module/endpoint renames, enum replacements,
    required-parameter value sources, operation->SDK-symbol bridges.
    The only way a rename/replacement becomes "known"; never guessed.
  - `classify.py` -- event-level risk + machine-readable reasons.
  - `events.py` -- build events from (A) two contract files, (B) a
    version pair with registry/discovery context, (C) SDK surface pair,
    (D) release metadata; stable fingerprint (observed_at excluded).
  - `consumers.py` -- change -> usage site -> enclosing symbol, per-diff
    item matching (exact SDK symbol, HTTP path/method, schema reference,
    import/module, auth, package-level). Unmatched sites are reported as
    ignored, never as affected.
  - `code_graph.py` -- local checkout -> existing graph primitives.
  - `blast_radius.py` -- DIRECT / TRANSITIVE / POTENTIAL, bounded depth,
    confidence per edge, related tests, modules.
  - `workspace.py` -- multi-repository impact (affected / unaffected /
    uncertain), local checkouts and registry-backed.
  - `store.py` -- idempotent persistence of events / diff items / impacts.
- `patchfrog/migration/` (M7) -- `MIGRATION_ENGINE_VERSION = 1`
  - `domain.py` -- `MigrationPlan`, `MigrationStep`, `MigrationTarget`,
    `MigrationStrategy`, `AutoFixEligibility`
    (AUTO_SAFE/AUTO_WITH_REVIEW/HUMAN_REQUIRED/UNSUPPORTED),
    `MigrationStatus` (PLANNED/PATCH_GENERATED/PARTIAL/HUMAN_REQUIRED/
    UNSUPPORTED/FAILED), `MigrationResult`, `PatchLinkage`.
  - `planner.py` -- per affected consumer x applicable diff item ->
    strategy + eligibility; values are never invented (no known source
    -> HUMAN_REQUIRED).
  - `python_rewrite.py` / `js_rewrite.py` / `manifest_rewrite.py` --
    AST-located (Python) / lexically-located (JS/TS) span edits; all
    other bytes untouched (formatting preserved).
  - `generator.py` -- pure: reads the checkout, never writes it; emits a
    unified diff; idempotent on already-migrated code.
  - `safety.py` -- deterministic gates (allowed files, edit->step
    correspondence, secret files, no dependency removal, size bound,
    syntax parse, obsolete shape gone).
  - `assist.py` -- optional model-assisted path behind `LLMProvider`,
    constrained to target spans, all gates applied, origin recorded;
    tests use `FakeLLMProvider` only; not wired to any CLI default.
  - `store.py` -- idempotent persistence of plans + patches + linkage.
- Persistence: one Alembic revision `0035_upstream_change_migration`
  (5 tables: `external_change_events`, `external_change_diff_items`,
  `external_change_impacts`, `migration_plans`, `migration_patches`).
  Full normalized contracts are *not* duplicated (M5 snapshots own them).
- CLI: `changes diff|analyze|demo`, `migrations plan|generate`.
- Fixtures: `tests/fixtures/upstream_changes/<case>/` with a
  `case.yaml` expectation per case (the 10 required cases + demo).

Out of scope (explicit): continuous watchers/polling (M11), network
fetches of specs/releases, runtime verification (M8), PR creation (M9),
Cloud changes (none required -- everything is engine-level).

Versions: `UPSTREAM_CHANGE_VERSION = 1` and `MIGRATION_ENGINE_VERSION = 1`
are new. Review/prompt/telemetry/cost-policy versions are untouched
(normal review is not modified). `CLAUDE.md` is user-owned and is not
edited; its version table will need these two rows added by its owner.

## RESULT

Baseline confirmed: `main` @ `c66c562` (M4+M5 squash merge). Branch:
`feat/m6-m7-upstream-change-migration`, 5 milestone commits
(`b414b64`, `ae65e98`, `719dac6`, `9ae14da`, `b9187cd`) plus this
completion pass's own commits (final SHAs below).

### M6 -- what was built

- **Contract-diff engine** (`patchfrog/upstream/openapi_diff.py`,
  `sdk_surface.py`, `package_version.py`): deterministic,
  consumer-direction-aware OpenAPI diff (a response widened is safe, a
  request widened is not), a small provider-agnostic SDK surface diff for
  SDKs with no OpenAPI contract, and semver-aware version comparison
  (a major bump alone is only ever POTENTIALLY_BREAKING, never asserted
  BREAKING on its own). `hints.py`'s `patchfrog_change_hints: 1` is the
  only path a rename/replacement is ever treated as known; hinted literal
  values are rejected if credential-shaped.
- **Consumer mapping** (`consumers.py`): each diff item matched against
  M5 usage sites by the same evidence kind it is about (SDK-call chain,
  HTTP path+method, schema reference, import/module, auth,
  package-level). An unmatched usage site is reported as ignored, never
  as affected -- verified directly by the `unaffected_consumer_trap`
  fixture case.
- **Blast radius** (`code_graph.py`, `blast_radius.py`): bounded-depth
  DIRECT/TRANSITIVE/POTENTIAL edges with a confidence per edge and
  related tests/modules, on the existing graph primitives. Python/C/C++
  get a real caller graph; JS/TS gets lexical usage sites only (no caller
  graph) -- reported explicitly, not silently under-reported.
- **Cross-repository impact** (`workspace.py`): `--registry` aggregates
  every repository the M5 registry already knows about (registry rows
  are real discovery evidence, never name-inferred) into
  affected/unaffected/uncertain sets; verified by the
  `multi_repo_dependency_usage` fixture (4 repositories, one clean).
- **Persistence** (`store.py`, migration `0035`): idempotent
  `external_change_events` / `external_change_diff_items` /
  `external_change_impacts`, fingerprint-keyed, full contracts not
  duplicated (M5 snapshots own them by fingerprint).
- **CLI**: `changes diff`, `changes analyze` (`--repo` local or
  `--registry`, `--persist`).

### M7 -- what was built

- **Planner** (`planner.py`): per affected usage site x diff item ->
  `MigrationStrategy` + `AutoFixEligibility`. Values are never invented;
  no deterministic/hinted source means `HUMAN_REQUIRED`, never a guess.
- **Deterministic rewrite** (`python_rewrite.py`/`js_rewrite.py`/
  `manifest_rewrite.py`/`edits.py`): AST-located (Python) or
  lexically-located (JS/TS) character-span edits; all other bytes
  untouched.
- **Generator + idempotency** (`generator.py`): pure (reads, never writes
  the checkout unless the CLI's `--write` is given), returns a unified
  diff. **Idempotency confirmed empirically**, not just asserted: running
  `migrations demo --write` once against a scratch copy of the bundled
  demo repo, then re-running `migrations generate` against that same
  now-migrated checkout, produces `modified_files: []`,
  `is_candidate: false`, an empty diff, and a plan with **zero steps**
  (the planner itself finds no remaining affected usage sites once the
  obsolete call shape is gone) -- a stronger guarantee than "the diff
  happens to be empty." The parametrized corpus test
  (`test_patch_is_idempotent_on_a_materialized_copy`) checks the same
  property across every fixture case with a materializable patch.
- **Safety gates** (`safety.py`, 8 gates): `allowed_files`,
  `edits_match_steps`, `no_secret_files`, `no_dependency_removed`,
  `bounded_diff`, `line_stability`, `syntax`, `obsolete_shape_gone`. A
  patch is a candidate only if every gate passes.
- **Model-assisted path** (`assist.py`): opt-in only, for a
  `HUMAN_REQUIRED` `add_required_parameter`/`replace_enum_value` step
  with a single already-located call; one structured value field only,
  same rewriter, same safety gates, own `PatchOrigin.MODEL_ASSISTED`,
  never merged with the deterministic patch; a hallucinated value fails
  the same `RewriteError` path a bad hint would. Tested with
  `FakeLLMProvider` only. A structural test
  (`test_deterministic_migration_modules_never_import_a_provider`) walks
  every other module's AST and asserts none imports
  `patchfrog.review.provider(s)` or an `anthropic`/`openai`/`google`
  package.
- **Persistence** (`store.py`): idempotent `migration_plans` (keyed on a
  fingerprint over the change plus a `base_content_fingerprint` --
  sha256 over every targeted file's content, since a local checkout has
  no commit SHA) and `migration_patches` (keyed on plan + patch
  fingerprint); `PatchLinkage` for a future M8/M9 to key on.
- **CLI**: `migrations plan`, `migrations generate` (`--write`,
  `--output-dir`, `--persist`), `migrations demo` (the bundled, fully
  offline, fictional `acme-ai` SDK -- never a real vendor).

### End-to-end demo (`python -m patchfrog.cli migrations demo`)

Upstream change: `chat.create(prompt=...)` -> `responses.create(input=...)`,
one argument removed (`stream`), one result field renamed
(`text` -> `output_text`), a major version bump -- classified `BREAKING`.
Across two consumer files the planner produced 8 steps (7 automatic: 3
`AUTO_SAFE` renames, 3 `AUTO_WITH_REVIEW` return-field adapts, 1
`AUTO_WITH_REVIEW` version bump; 1 `HUMAN_REQUIRED` for the removed
`stream` argument, correctly left for a human since dropping an input
changes behavior). The generated patch applied 7/8 edits, all 8 safety
gates passed, and the emitted unified diff is a minimal, correct rewrite
of both call sites plus the manifest version bump -- reviewed by hand
above and matches expectation exactly.

### Full validation results

| Gate | Result |
|---|---|
| Full pytest suite, real Postgres (`PATCHFROG_REQUIRE_POSTGRES=1`) | **2810 passed, 5 skipped** (expected no-op fixture cases), 655s |
| ruff (repo-wide) | clean |
| mypy --strict (repo-wide) | clean, 734 source files |
| Alembic | single head (`0035_upstream_change_migration`); fresh `alembic upgrade head` from scratch succeeds on real Postgres |
| Docker build, `api` target | succeeds (content-hash cache correctly picked up working-tree changes; verified new `patchfrog.migration.{store,assist,report}` modules present inside the built image) |
| Docker build, `worker` target | succeeds |
| Celery task registration (in-container, mirrors CI exactly) | 9/9 tasks registered: `patchfrog.{analyze_repository, build_context, index_repository, process_pull_request_event, publish_review, review_pull_request, run_review_pipeline, sync_installation_event, sync_installation_repositories_event}` |
| Migration CLI integration tests | 8/8 passed |
| M6/M7-specific suites (dependency discovery, contract-diff, consumer/blast-radius, migration planner/generator) | 35 + 35 + 48 + 75 passed (5 skipped, expected), isolated re-run |
| Secret-pattern scan, branch diff only (`main...HEAD` + working tree) | no private-key blocks, no AWS/GitHub/Slack/Google token-shaped strings, no credential-shaped assignments; the one pattern hit is a unit test (`test_hints_reject_credential_shaped_values_and_bad_shapes`) asserting such values are *rejected* using obviously synthetic placeholders. **Scope note: this proves tracked-file/diff content only, not absence of terminal/UI/local display exposure.** |
| Stray/temporary files | none found; working tree contains exactly the expected new/modified file set |

### M4/M5 regression status (no regression)

| Guard | Result |
|---|---|
| M4 cost benchmark (`eval cost-benchmark`) | identical to the pre-existing baseline table: 23->10 calls, 42930->20383 tokens, $0.046218->$0.022343, 2/2 accepted findings preserved, all targets met |
| Beta-readiness eval (`eval run --beta-readiness --repeat 2`) | expectation pass rate 1.0, candidate recall 1.0, accepted-finding recall 1.0, false-positive/false-negative/critic-false-negative rate 0.0, repeated-run variance 0.0 -- identical to the M4 baseline; reviewer calls summed to 22, critic calls to 10 (matches the pre-M6/M7 total exactly, confirming M6/M7 added zero calls to the normal review path) |
| Dependency discovery suite | 35 passed |
| Contract-diff suite | 35 passed |
| Impact/blast-radius suite | 48 passed |
| Migration planner/generator suite | 75 passed, 5 skipped (expected) |

### Known limitations

- JS/TS blast radius has no caller graph (usage sites only) -- stated
  explicitly in output, not silently under-reported; a real gap, not a
  bug.
- Cross-repo impact (`workspace.py --registry`) is bounded by what the
  M5 registry has actually discovered; it is not a live, network-fetched
  view of every consumer that could exist.
- `assist.py`'s model-assisted path is exercised only against
  `FakeLLMProvider` in this milestone -- no live provider call was made
  (per project policy, no live LLM call without explicit approval).
- Migration verification is structural/deterministic only (the 8 safety
  gates); there is no executable/runtime proof a generated patch actually
  fixes the original break -- that is M8, not started.
- No PR is opened from a generated patch -- that is M9, not started.
- The tracked-file secret scan (this milestone and every prior one) never
  proves absence of terminal/UI/local display exposure, only tracked-file
  content.

### Milestone completion

- **M6 is complete** for its stated scope (contract diff, consumer
  mapping, blast radius, cross-repo impact via the registry,
  persistence, CLI) -- no deferred M6 item remains open.
- **M7 is complete** for its stated scope (planner, deterministic
  rewrite, idempotent generator, 8 safety gates, optional narrow
  model-assisted path, persistence, CLI, bundled demo) -- no deferred M7
  item remains open.
- **Ready for M8 (Executable/Runtime Verification): yes, architecturally.**
  `PatchLinkage` and the persisted plan/patch fingerprints already give
  M8 exactly what it needs to key a verification run on a specific
  generated patch; the existing `executable_verification`/
  `fix_verification` sandbox (S/S6) is the primitive M8 would extend
  rather than replace. M8 itself has not been started and needs its own
  audit-first pass (per the workflow) before implementation, in
  particular to decide how a migration's verification target (which
  test, which entry point) is determined when the patch has no
  associated PR yet.
