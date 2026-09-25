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
