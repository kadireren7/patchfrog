# Progress

_Last updated: 2026-11-23 (Phase 2, Milestone 2.5 session). Update at the end of
every implementation session (see `CLAUDE.md` → Session workflow)._

## Current phase

| Phase / milestone | Status |
|---|---|
| Phase 1 (1.1–1.3) | ✅ complete locally; first CI run pending the owner's push |
| Phase 2, Milestone 2.1 | ✅ **verified**. It was CONDITIONALLY ACCEPTED (D7) while its full regression was deferred; the deferred coverage then ran in the 2.2 full regression (3,247 collected, 0 not executed, only S2), so B-2.1-REG is closed |
| Phase 2, Milestone 2.2 | ✅ **complete**: JobRunner implemented; gates, targeted tests and the full regression green (only S2) |
| Phase 2, Milestone 2.3 | ✅ **complete and verified**: criteria 1–8 met; the full regression finished on resume (3,292 collected = 3,233 passed + 54 skipped + 5 S2, 0 not executed); B-2.3-REG closed |
| Phase 2, Milestone 2.4 | **implementation complete; acceptance criteria 1–7 met; full regression NOT executed** (host memory never reached the guard threshold in two attempts). Blocker B-2.4-REG |
| Phase 2, Milestone 2.5 | **implementation complete; acceptance criteria 1–6 met; full regression NOT executed** (not attempted, by owner instruction; CI responsibility). Blocker B-2.5-REG |
| Phase 2 overall | all planned deliverables implemented (2.1–2.5); full-suite verification of 2.4–2.5 pending (first CI run) |

## Owner decisions

| ID | Decision | Recorded in |
|---|---|---|
| D1 | Architecture accepted. No repo-wide cycle cleanup; enforce only the core's boundaries; no new cycles | `DECISIONS.md` header |
| D2 | G1 non-blocking (own projects); blocking external gate G2 after the first complete MCP workflow | ADR-016, `ROADMAP.md` |
| D3 | Final license Apache-2.0; audit done; license unchanged | ADR-013, `docs/legal/APACHE_2_0_MIGRATION.md` |
| D4 | Review/compat lines frozen for features; maintenance, critical and security fixes continue; no deletions | ADR-001 |
| D5 | Check downstream (Cloud) impact before changing extras, interfaces, imports, dependencies or persistence | ADR-015 |
| D6 (2026-11-22) | The private consumer import manifest must not live in the public repo. Public tests verify documented engine interfaces only; the private consumer verifies its own usage in its own CI | ADR-017 |
| D7 (2026-11-22) | One-time conditional full-regression exception for Milestone 2.1 only (host memory). Missing coverage stays blocker B-2.1-REG before public release/Cloud deployment; not carried into later milestones | this file, `ROADMAP.md` 2.1 |

## Milestone 2.5 — public CLI and discovery schema

Definition and acceptance criteria: `ROADMAP.md` → Phase 2 → 2.5, written at the start of the
session from the owner's brief. User docs: `docs/scale-cli.md`. Design: `DISCOVERY.md` §10.

### Files
| File | Change |
|---|---|
| `patchfrog/cli_scale.py` (new) | `scale analyze/show/schema`: a thin adapter (arguments → `run_discovery` → text/JSON rendering), exit codes, user-level store resolution |
| `patchfrog/__main__.py` (new) | `python -m patchfrog`: dispatches `scale` without importing the full CLI; everything else goes to `patchfrog.cli.main` |
| `patchfrog/cli.py` | **4 lines:** import `cli_scale`, `cli_scale.register(subparsers)`, dispatch `scale`. No other command touched |
| `patchfrog/scalability/project/schema.py` (new) | JSON Schema generated from the dataclasses |
| `docs/schemas/application-model.v2.schema.json` (new) | Committed snapshot (25 KB), regenerated and compared by a test |
| `patchfrog/scalability/project/domain.py` | `from_dict` rejects unknown fields (strict like the schema) |
| `patchfrog/app/discovery.py` | `DiscoveryArtifactError`/`UnsupportedDiscoverySchemaError`; `load_discovery` never returns or leaks a corrupt artifact |
| `patchfrog/app/store.py` | **additive:** `RunNotFoundError(StoreError)` for a missing run |
| `tests/integration/test_scale_cli.py` (new, 10) | End to end through `python -m patchfrog scale` subprocesses (and once `python -m patchfrog.cli scale`) |
| `tests/unit/test_discovery_schema.py` (new, 38) | Snapshot equality, determinism, closed objects, 27 corpus artifacts validate and round-trip, v1 loads, 8 invalid kinds rejected by both schema and loader |
| `tests/architecture/public_api.py`, `PUBLIC_API.md` | API 0.6.0: +2 provisional (62: 18 / 41 / 3) |
| `scripts/bench_scale_cli.py` (new) | CLI benchmark (startup, first/repeat, per-child peak RSS, artifact size) |
| Docs | `docs/scale-cli.md` (new, user-facing), `DISCOVERY.md` §10, `LOCAL_STORE.md` (location), ADR-002 note, ADR-018 (*Proposed*: console script), `README.md` (a two-line pointer), `ROADMAP.md`, this file |

### Decisions and findings
- **Store location:** `--store` → `$PATCHFROG_STORE_DIR` → `$XDG_CACHE_HOME/patchfrog/store` →
  `~/.cache/patchfrog/store`, **not `CoreSettings`**, because `CoreSettings` reads the working
  directory's `.env`, which is repository-controlled when the CLI runs inside a repository.
  Tested: a repository `.env` with `PATCHFROG_STORE_DIR` doesn't redirect writes.
- **Fast path:** `python -m patchfrog scale --help` takes 0.15 s, against 3.5 s for
  `python -m patchfrog.cli`. The probe test asserts that no SQLAlchemy, FastAPI, httpx, Celery,
  Redis, LLM, persistence, review, server-settings or `patchfrog.cli` module is loaded. A
  `patchfrog` console script is a packaging change: ADR-018, *Proposed*, recommended for Phase 6.
- **Defects found while building the CLI and fixed:**
  - a missing run looked "corrupt" (exit 6) because the store raised a bare `StoreError`. Fixed
    with the additive `RunNotFoundError`; a missing run is exit 5;
  - the loader tolerated unknown fields that the schema rejects, a silent drift. The loader is
    now strict, and a test compares the two on 8 kinds of invalid document;
  - the first benchmark draft reported a cumulative child RSS, not a per-analysis one. It now
    measures each child's own peak.
- **No scores or estimates:** output states "Static structure only: no performance findings
  yet". `complete` is true only for a SUCCEEDED run with complete coverage.

### Measurements (`scripts/bench_scale_cli.py --repo .`, ~0.8 GB free, noisy)
| Measurement | Result |
|---|---|
| Startup `python -m patchfrog scale --help` / `python -m patchfrog.cli scale --help` | 0.15 s / 3.50 s (median of 5) |
| Small fixture app: first / repeat | 0.22 s / 0.17 s; peak RSS 25 MiB; artifact 5.6 KiB |
| This repository (1,145 Python files): first / repeat (reused) | 3.32 s / 0.34 s; peak RSS 156 / 31 MiB; artifact 423 KiB |
| Same with `--exclude tests/fixtures` | 2.84 s / 0.33 s; peak RSS 152 MiB; artifact 306 KiB |

### Tests executed (final file versions)
| What | Command | Result |
|---|---|---|
| Ruff | `.venv/bin/ruff check .` | **pass** |
| mypy strict | `.venv/bin/mypy . --strict` | **pass** (874 source files) |
| New CLI + schema, all discovery, JobRunner/store, architecture/public API, CLI unit and startup tests | `.venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/ tests/integration/test_scale_cli.py tests/unit/test_discovery_schema.py tests/unit/test_database_discovery.py tests/unit/test_database_corpus.py tests/unit/test_scalability_discovery.py tests/unit/test_discovery_corpus.py tests/unit/test_app_jobs.py tests/integration/test_app_jobs_processes.py tests/unit/test_app_revision.py tests/unit/test_app_local_store.py tests/integration/test_app_store_processes.py tests/unit/test_codegraph_local.py tests/unit/test_cli.py tests/unit/test_cli_lazy_worker_imports.py tests/unit/test_cli_local_startup.py tests/unit/test_core_settings.py tests/unit/test_dependency_manifests_and_scan.py` | **297 passed**, 0 failed, 0 skipped |
| Existing CLI integration regression | `.venv/bin/python -m pytest -q -p no:cacheprovider tests/integration/test_dependency_cli.py tests/integration/test_migration_cli.py tests/integration/test_migration_publish_cli.py tests/integration/test_migration_verify_cli.py tests/integration/test_upstream_cli.py tests/unit/test_evaluation_cli_filters.py` | **31 passed, 6 skipped**. The skips are the known environmental real-Postgres tests (`tests/support/postgres.py`: no local PostgreSQL) |
| **Full regression** | not attempted in this session | **NOT EXECUTED**, by the owner's instruction (no repeated memory-unsafe full runs on this host; the full suite becomes a CI responsibility). The guard thresholds are unchanged |

### Milestone 2.5 status: **implementation complete; full-regression gate NOT green**
| # | Acceptance criterion | Status / evidence |
|---|---|---|
| 1 | `scale analyze`: local-only, reuses `run_discovery`/JobRunner/store, reports structure, unresolved and completeness, distinguishes partial, prints the run id, meaningful exit codes, executes nothing | ✅ e2e tests 1–4, 7 |
| 2 | `scale show`: validates ids, rejects corrupt, foreign-kind and unsupported runs, JSON output, handles missing/interrupted/partial/failed, never reports incomplete as complete | ✅ e2e tests 4–6 |
| 3 | Versioned JSON Schema: one source, deterministic, required/nullable/enum semantics, v1 compatible, future rejected, snapshot and round-trip tests | ✅ `test_discovery_schema.py` (38) |
| 4 | Thin adapter, no second framework, existing CLI intact, startup addressed without a refactor | ✅ 4-line `cli.py` change; fast path; existing CLI tests green |
| 5 | Docs and examples (verbatim), no scores | ✅ `docs/scale-cli.md` |
| 6 | Performance measured, not claimed | ✅ table above |
| 7 | Gates green; full regression | ⚠️ ruff/mypy/architecture/targeted ✅; full regression **not executed** (B-2.5-REG) |

### Git commit-readiness (read-only, 2026-11-23; nothing staged, committed or pushed)
- **State:** branch `main` at `9b9de85`. 16 tracked files modified or deleted (+225/−520 lines)
  and 170 untracked files (~0.9 MB, no binaries, no generated artifacts: no `__pycache__`, store,
  logs or coverage files outside ignored paths). `git diff --check` is clean, and there is no
  trailing whitespace in the untracked text files.
- **Ignored private material:** `/private/` (the Cloud handoff) is git-ignored and must stay out
  of the commit. The owner still needs to move it into the private repository (ADR-017).
- **Secrets:** no token, key or private-key patterns. The only credential-shaped strings are
  **deliberately fake** test values (`app:s3cret@db.internal`, `admin:hunter2@db.example.invalid`,
  `u:p@localhost`, the CI's own `patchfrog:patchfrog@localhost`). They exist to prove that
  credentials are never retained. A strict secret scanner may flag them; that is acceptable or
  easy to allowlist.
- **Private Cloud data:** no import manifests, engine pins or implementation details. The private
  repository slug in `CURRENT_STATE.md` already appears in committed docs
  (`product-boundary.md`, `governance-policy.md`), so this is not a new exposure.
- **Docs consistency:** the old `docs/roadmap.md` moved to `docs/history/`, and
  `docs/architecture.md` became `docs/compatibility-pipeline.md` (DECISIONS.md). Remaining
  references to the old names are in historical `validation/*` summaries, which are not rewritten
  by rule.
- **CI:** `ci.yml` runs `pip install -e ".[dev]"`, ruff, `mypy --strict` and `pytest -q` with
  real Postgres and Redis. That is the full suite, and it would close B-2.4-REG and B-2.5-REG.
  The new tests need only `git` (present on CI runners) and `jsonschema`, which arrives
  transitively through the base dependency `mcp` (`jsonschema>=4.20.0`), with `importorskip` as
  a fallback. There are no timing assertions.
- **Recommendation:** ready for a **controlled commit and CI run**, with these conditions:
  - commit on a feature branch, not directly on `main`;
  - keep `/private/` out of the commit (already ignored);
  - treat the first CI run as the full-regression gate for 2.4 and 2.5;
  - don't release or deploy until CI is green (beyond the known S2 behaviour, which is CI-only
    Redis, so it should pass there).
- **Known residual risk for that first CI run:** Phase 1–2 have never run in CI with real
  Postgres/Redis. The last complete local suite run (2.3 tree, 3,292 tests) had only S2 failures,
  but CI is the first run with the 2.4 and 2.5 changes. No technical blocker to committing is
  known.

## Milestone 2.4 — SQLAlchemy / SQLModel structure discovery

Definition and acceptance criteria: `ROADMAP.md` → Phase 2 → 2.4, written at the start of the
session from the owner's brief. Spec, with every supported and unsupported pattern:
`docs/architecture/DISCOVERY.md` §9.

**Before any 2.4 change**, the recorded 2.3 regression plan was resumed from batch 98 without
resetting completed batches. It finished (3,292 accounted for, only S2), so B-2.3-REG is closed
(see Milestone 2.3).

### Files
| File | Change |
|---|---|
| `patchfrog/scalability/project/db_domain.py` (new) | Typed model: `DatabaseModel`, bases, models, columns, relationships, engines/factories/sessions, providers, route → session links, `DbUnresolved`; provenance enums |
| `patchfrog/scalability/project/sqlalchemy_models.py` (new) | `DatabaseDiscovery(RouteDiscovery)`: classification, attribute extraction, relationship resolution, constructions, providers, route links |
| `patchfrog/scalability/project/fastapi_routes.py` | **Extension hooks** (`_construct`, `_call_value`, `_attribute_value`, `_enter`, `_returnable`), an `_Object` value base, evaluable `with` targets, `imported_roots`, an overridable candidate pattern, post-assembly unresolved/limitations, `max_models`. Route behaviour unchanged (all 2.3 tests and corpus unchanged and green) |
| `patchfrog/scalability/project/domain.py` | `ApplicationModel.database`; `DISCOVERY_SCHEMA_VERSION = 2` (version 1 still loads) |
| `patchfrog/scalability/project/discovery.py`, `__init__.py` | Database pass wired in; `db_unresolved:<kind>` completeness reasons; `DatabaseModel` export |
| `patchfrog/app/discovery.py` | `DISCOVERY_RULESET_VERSION = "fastapi.1+sqlalchemy.1"` |
| `tests/fixtures/scalability/database/cases/` (new, 13 cases, 38 files) | Independent corpus with hand-written `expected.json` |
| `tests/unit/test_database_corpus.py` (new, 15) | Evaluator (P/R per supported category, exact unresolved/bases/complete) |
| `tests/unit/test_database_discovery.py` (new, 22 incl. 5 parametrized) | Determinism, schema v1 compatibility, secrets, 1,000-model scale with a traced-memory bound, `max_models`, unresolved limit, malformed file, ambiguous base import, duplicate names across registries, no import/execution (models and Alembic `env.py`), symlinks, change after capture, cancellation during analysis, idempotency/reproducibility, provider provenance, empty-env subprocess, annotation targets, regression for the StrEnum false positive |
| `tests/architecture/public_api.py`, `docs/architecture/PUBLIC_API.md` | API 0.5.0: +1 provisional (`DatabaseModel`; 60 names: 18 / 39 / 3) |
| `scripts/bench_discovery.py` | ORM fixture added |
| Docs | `DISCOVERY.md` (header, §5, §6, new §9), `ROADMAP.md` (2.4 definition), ADR-004 implementation note (2.4), `DETECTION_RULES.md` (prerequisites 4–5 status), `BOUNDARIES.md` (2.4 measurement), this file |

No review, migration, persistence or Cloud code was touched. There is no database-schema change and
no new dependency.

### Findings during implementation
- **Recall defect found by the corpus and fixed:** engines and factories that nothing referenced
  were not reported (objects 5/7, options 7/8 on first evaluation). Named constructions are now
  evaluated up front.
- **Defect found through a missed mutation and fixed:** after a non-literal `primary_key=`, the
  same call's `ForeignKey`/`nullable` were not read. A corpus case was added, and the mutation is
  now detected.
- **False positive found by the real-world spot check and fixed:** a `StrEnum` whose *values* were
  ORM words was reported as a "possible model". The check is now structural (regression test).
- **Lost entry found by a unit test and fixed:** an ambiguous import first met by the database
  pass was dropped because it was recorded after route assembly. Post-assembly entries are now
  merged.
- **Real-world spot check (not a metric):** this repository's `patchfrog/persistence` has 45
  models. Discovery finds exactly 45 (`__tablename__ =` lines counted by hand: 45), with nothing
  unresolved outside the test fixtures.

### Corpus results (fixture agreement only; not evidence of real-world accuracy)
13 cases. Every category has precision = recall = 1.00:
models 20/20, columns 34/34, relationships 13/13, engines/factories/sessions 7/7, options 8/8,
providers 6/6, route → session links 7/7. Unresolved entries, bases and `complete` matched exactly
in all 13 cases. Evaluator sensitivity: 10 of 10 engine mutations detected. The 2.3 FastAPI corpus
is unchanged (routes 36/36, dependencies 12/12).

### Measurements (`scripts/bench_discovery.py --repo .`, under tracemalloc, ~0.75 GB free, noisy)
- Synthetic ORM app, 605 files (300 models, 300 routes, 300 route → session links): 1.43 s,
  repeat 0.10 s, traced peak 13.4 MiB.
- 4,005 files (2,000 models): 9.74 s, repeat 0.77 s, traced peak 79.1 MiB.
- This repository: 7.03 s under tracemalloc, 3.3–4.0 s without (1.2 s in 2.3, because more files
  are parsed: 418 vs 64); traced peak 118.1 MiB.
- Process peak RSS 293 MiB.
- A 6,005-file app hits `max_python_files` and is correctly PARTIAL.
- One measured hot spot was rewritten with no measurable gain; nothing further was optimized.
- Import graph: 513 modules, 3,189 edges, 53 units, 306 unit edges (unchanged). The historical
  SCC is unchanged at 29/156.

### Tests executed (final file versions)
| What | Command | Result |
|---|---|---|
| Ruff | `.venv/bin/ruff check .` | **pass** |
| mypy strict | `.venv/bin/mypy . --strict` | **pass** (868 source files) |
| 2.4 + 2.3 + 2.2 + 2.1 targeted + architecture/public API + manifest parser | `.venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/ tests/unit/test_database_discovery.py tests/unit/test_database_corpus.py tests/unit/test_scalability_discovery.py tests/unit/test_discovery_corpus.py tests/unit/test_app_jobs.py tests/integration/test_app_jobs_processes.py tests/unit/test_app_revision.py tests/unit/test_app_local_store.py tests/integration/test_app_store_processes.py tests/unit/test_codegraph_local.py tests/unit/test_cli_local_startup.py tests/unit/test_core_settings.py tests/unit/test_dependency_manifests_and_scan.py` | **244 passed**, 0 failed, 0 skipped |
| Corpus mutation check | 10 engine mutations, `pytest tests/unit/test_database_corpus.py -k matches` | 10/10 detected |
| **Full regression** | two attempts, each: wait up to 600 s for MemAvailable ≥ 1,500 MB sustained 30 s, then `MIN_START_MB=1500 .venv/bin/python scripts/safe_pytest.py plan/run/report <scratch>/pf-run24` | **NOT EXECUTED.** The condition was never met (attempt 1: host minimum 387 MB; attempt 2: minimum 191 MB, maximum 1,630 MB but never for 30 s). No plan was created and no test from the full plan ran. The guard was not bypassed or lowered |

### Milestone 2.4 status: **implementation complete; full-regression gate NOT green**
| # | Acceptance criterion | Status / evidence |
|---|---|---|
| 1 | Provenance distinguished, nothing guessed | ✅ corpus columns/relationships/models; non-literal primary-key case |
| 2 | No import/execution/migration/connection; only the URL scheme kept | ✅ marker and empty-env tests; secrets test |
| 3 | Dynamic/unsupported/unresolvable → `DbUnresolved`, model incomplete | ✅ cases 08–10, 12, 13 |
| 4 | Corpus ≥ 12 cases, P = R = 100% on supported patterns | ✅ 13 cases, all 7 categories 1.00 |
| 5 | Determinism, scale, memory, cancellation, revision change, malformed, symlinks, duplicates, ambiguity | ✅ `test_database_discovery.py` |
| 6 | JobRunner integration; version-1 artifacts readable | ✅ |
| 7 | R1, no new cycles, minimal API | ✅ 30/30 architecture tests; +1 provisional name |
| 8 | Gates green; full regression | ⚠️ ruff/mypy/architecture/targeted ✅; full regression **not executed** (host memory) |

The missing coverage is tracked as **B-2.4-REG**. No exception was requested or applied (D7
covered 2.1 only).

## Milestone 2.3 — application discovery (FastAPI)

The definition and acceptance criteria are in `ROADMAP.md` → Phase 2 → 2.3. They were written
before implementation and confirmed against the Phase 2 objective and ADR-004. The spec, with
every supported and unsupported pattern, is in `docs/architecture/DISCOVERY.md`.

### Files
| File | Change |
|---|---|
| `patchfrog/scalability/__init__.py`, `patchfrog/scalability/project/{__init__,domain,frameworks,fastapi_routes,discovery}.py` (new) | Typed model, framework evidence, static route discovery and the pure orchestrator. stdlib `ast` only. The only internal imports are `dependencies.{domain,manifests}` |
| `patchfrog/app/discovery.py` (new) | `run_discovery` / `discovery_job` / `load_discovery` as a JobRunner job, with a budgeted, revision-verified reader |
| `patchfrog/app/revision.py` | **additive:** `read_revision_file` and `RevisionEntryChanged` |
| `patchfrog/app/jobs.py` | **additive:** `JobContext.read_file` (the runner passes the root) |
| `tests/fixtures/scalability/discovery/cases/` (new, 14 cases, 64 files) | Evaluation corpus with hand-written `expected.json` |
| `tests/unit/test_discovery_corpus.py` (new, 16) | Corpus evaluator: precision/recall plus exact checks (`python -m tests.unit.test_discovery_corpus` prints the table) |
| `tests/unit/test_scalability_discovery.py` (new, 29) | Version rules, identities and serialization, cycles, star and namespace imports, parse errors, lazy parsing, every limit, idempotency, immutability, reproducibility across locations, cancellation, wall-time and byte budgets, change after capture, foreign revision, no import/execution, symlinks, concurrency, empty-env run, 300-file layered fixture |
| `tests/architecture/rules.py` | `patchfrog.scalability` added as an R1 core root |
| `tests/architecture/public_api.py`, `docs/architecture/PUBLIC_API.md` | API 0.4.0: +4 **provisional** names (59: 18 stable / 38 provisional / 3 deprecated) |
| `pyproject.toml` | **tool configuration only:** `tests/fixtures/scalability` excluded from pytest collection, ruff and mypy, like the existing fixture directories. No dependency, extra or package change |
| `scripts/bench_discovery.py` (new) | Bounded benchmark (developer tool) |
| Docs | `DISCOVERY.md` (new); `ROADMAP.md` (2.3 definition and status); ADR-004 implementation note; `BOUNDARIES.md` (R1 roots, 2.3 measurement); `OVERVIEW.md`; `DETECTION_RULES.md` (prerequisites 1–2 done); `LOCAL_STORE.md` (`read_file`); `CLAUDE.md` (core-root list, key docs); this file |

No existing review, migration or compatibility module was touched, and no new runtime dependency
was added.

### Findings during implementation
- **Performance defect found by the benchmark and fixed:** namespace-package lookups scanned every
  module name for each external import, which was quadratic. The 3,005-file fixture took 12.9 s.
  A precomputed prefix set brought it to 3.25 s (linear).
- **Correctness defects found while designing the corpus and fixed before evaluation:**
  `from . import users` inside a package `__init__` resolved through itself, two conditional
  `APIRouter()` bindings of one name shared an id (which would have hidden the ambiguity), and
  instances of external classes used as dependencies were reported as unresolved instead of
  external.
- **Real-world spot check (not a metric):** on this repository, `apps/api` (an app factory and 3
  routers with stacked decorators) gives exactly the 5 routes counted by hand, with nothing
  unresolved.

### Corpus results (fixture agreement only; not evidence of real-world accuracy)
14 cases (2 negative controls, 4 uncertainty cases). Routes **36/36, P = R = 1.00**; dependencies
**12/12, P = R = 1.00**. Unresolved references, limitations, framework evidence and `complete`
matched exactly in all 14. The evaluator is sensitive: **7 of 7** engine mutations were detected.

### Measurements (`scripts/bench_discovery.py --repo .`, under tracemalloc)
- 305-file layered fixture: 0.32 s, repeat 0.03 s (reused), traced peak 4.2 MiB.
- 3,005 files: 3.25 s, repeat 0.24 s, traced peak 33.2 MiB.
- This repository (1,116 Python files, 64 parsed): 1.22 s.
- Process peak RSS: 104 MiB.
- Import graph: 511 modules, 3,177 edges, 53 units, 306 unit edges. The historical SCC is
  unchanged (29/156), and `scalability` is in no cycle.

### Tests executed (final file versions)
| What | Command | Result |
|---|---|---|
| Ruff | `.venv/bin/ruff check .` | **pass** |
| mypy strict | `.venv/bin/mypy . --strict` | **pass** (864 source files) |
| 2.3 + 2.2 + 2.1 targeted + architecture/public API + manifest parser tests | `.venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/ tests/unit/test_scalability_discovery.py tests/unit/test_discovery_corpus.py tests/unit/test_app_jobs.py tests/integration/test_app_jobs_processes.py tests/unit/test_app_revision.py tests/unit/test_app_local_store.py tests/integration/test_app_store_processes.py tests/unit/test_codegraph_local.py tests/unit/test_cli_local_startup.py tests/unit/test_core_settings.py tests/unit/test_dependency_manifests_and_scan.py` | **207 passed**, 0 failed, 0 skipped |
| Corpus mutation check | 7 engine mutations, `pytest tests/unit/test_discovery_corpus.py -k matches` | 7/7 detected |
| **Full regression** | `MIN_START_MB=1500 .venv/bin/python scripts/safe_pytest.py {plan,run,report} <scratch>/pf-run23` (guards unchanged) | **partial.** 3,292 collected (= 3,247 + 45 new), 115 batches. Batches 0–97 ran: **2,045 passed + 54 skipped + 5 failed, 0 errors, 0 duplicates.** The 5 failures are exactly the known environmental S2 (`test_ops_doctor.py`). **No new regression in the executed tests.** Batch 98 was refused by the guard (MemAvailable 1,165 MB < 1,500). **1,188 tests not executed** (batches 98–114: 101 files in `tests/unit`, `test_migration_generator.py` … `test_worker_task.py`, which includes `test_scalability_discovery.py`, already passed in the targeted run above). Peak pytest tree RSS 269 MB |
| Resume attempt 1 | waited up to 600 s for MemAvailable ≥ 1,500 MB sustained 30 s | **not resumed** (host minimum 401 MB). The guard was not bypassed or lowered |
| **Resume attempt 2 (2.4 session, before any 2.4 change)** | same wait, then `MIN_START_MB=1500 .venv/bin/python scripts/safe_pytest.py run <scratch>/pf-run23 --start 98` and `report` on the **same plan**; completed batches were not reset | **completed.** Batches 98–114 all ran. Whole plan: **3,292 collected = 3,233 passed + 54 skipped + 5 failed; 0 errors, 0 not executed, 0 duplicates, 0 ran-but-not-collected.** Only the known S2 failures. **No new regression.** No source or test file had changed since the plan was created (checked with `find -newer plan.json`) |

No source or test file changed after the full-plan run started.

### Milestone 2.3 status: ✅ **complete and verified** (full regression finished on resume, 2.4 session)
| # | Acceptance criterion | Status / evidence |
|---|---|---|
| 1 | No version invented | ✅ `test_version_rules_never_invent_an_installed_version`, corpus cases 01/02/03/13/14 |
| 2 | Recall/precision 100% on ≥ 12 fixture apps | ✅ 14 cases, 36/36 routes, 12/12 dependencies |
| 3 | Unresolved never becomes a route; `complete` honest | ✅ cases 08/09/12, mutation "guess dynamic path" detected |
| 4 | No import/execution, no symlinks, revision-verified reads | ✅ safety tests |
| 5 | Every bound explicit and tested | ✅ limit tests |
| 6 | JobRunner: idempotency, cancellation, reproducible artifacts | ✅ |
| 7 | ~300-file fixture (trend < 10 s) | ✅ 0.32 s |
| 8 | Gates green; full regression | ✅ ruff/mypy/architecture/public API; full regression 3,292 accounted for, only S2 (completed on resume) |

No exception was used: D7 covered 2.1 only. B-2.3-REG was open until the resume completed.
Pre-resume assessment, kept for the record: the unexecuted files are existing unit tests of the
frozen lines. 2.3 changed no module they import (only `patchfrog/app/{jobs,revision}.py`
additively, which nothing outside `patchfrog/app` imports, and test-tool excludes in
`pyproject.toml`).

## Milestone 2.2 — in-process JobRunner

Definition and acceptance criteria: `ROADMAP.md` → Phase 2 → 2.2 (written down before
implementation in this session, from the approved scope in the previous "Next actions"). Design:
`docs/architecture/LOCAL_STORE.md` §3.

### Files
| File | Change |
|---|---|
| `patchfrog/app/jobs.py` (new) | `JobRunner`, `JobSpec`, `JobBudget`, `JobContext`, `JobResult`, `JobReport`, `BudgetUsage`, `JobError`, `FailureClass`, `CancellationToken`, `config_fingerprint`, `derive_idempotency_key` |
| `patchfrog/app/store.py` | **additive only:** `locks/` directory and the `request_lock()` method (256 striped `flock` files). No existing behaviour, layout file or schema version changed |
| `patchfrog/app/__init__.py` | docstring |
| `tests/unit/test_app_jobs.py` (new, 43) | key determinism and sensitivity, config validation, reuse without calling the job, no reuse of PARTIAL/FAILED/CANCELLED, scope/content/version changes, store inside the root, all failure classes, no leaked exception messages, swallowed-stop guards, KeyboardInterrupt, artifact name rules, cancellation (before start, after the last checkpoint, from another thread), wall-time/artifact-count/artifact-bytes budgets with a fake clock, revision budget and bad root, 4-thread single execution, bounded lock wait, lock stripes, empty-env run without server modules |
| `tests/integration/test_app_jobs_processes.py` (new, 2) | real SIGKILL during a job → INTERRUPTED and recomputed; 3 processes, identical request → job runs once |
| `tests/architecture/public_api.py`, `docs/architecture/PUBLIC_API.md` | API 0.3.0: +10 **provisional** names (55: 18 stable / 34 provisional / 3 deprecated) |
| Docs | `ROADMAP.md` (2.1 closure, 2.2 definition), `LOCAL_STORE.md` (§3 new; §4–5 renumbered), `SCALABILITY.md` §2/§4, `OVERVIEW.md` §4, `DECISIONS.md` (ADR-010 implementation note), `MIGRATION_PLAN.md` (M5), `BOUNDARIES.md` (2.2 measurement), `TEST_STRATEGY.md` (section reference), `CLAUDE.md` (core-root description), this file |

No existing production module outside `patchfrog/app/` was touched. No new dependency was added
(stdlib only). No CLI, MCP, rules, project model, queue or Cloud change.

### Design decisions (reversible; ADR-010 implementation note)
- Synchronous, in the calling thread. There is no `queued` state yet; it comes with the bounded MCP queue (Phase 4).
- The key covers the job protocol version, kind, `revision_id` (which includes the scope), the canonical config fingerprint, the engine version and the ruleset version. **Budgets are excluded.**
- Identical requests are serialized by `LocalRunStore.request_lock`, with a re-check inside the lock, so the job runs exactly once.
- A cancelled run never keeps the job's artifacts. A budget stop is PARTIAL (`budget_exhausted`) and keeps what was written. Internal errors record only the exception type.
- Stops are `BaseException`s, so a job's `except Exception` cannot hide them. This was proven by mutation: 5 mutations (no lock, no final cancel check, keep artifacts on cancel, stop as `Exception`, reuse any run) were each detected by the tests.

### Measurements
Import graph: 504 modules, 3,143 edges, 52 units, 304 unit edges (unchanged). Historical SCC
unchanged at 29 units / 156 edges. Full-suite peak pytest process-tree RSS: 272 MB.

### Tests executed (final file versions)
| What | Command | Result |
|---|---|---|
| Ruff | `.venv/bin/ruff check .` | **pass** |
| mypy strict | `.venv/bin/mypy . --strict` | **pass** (854 source files) |
| 2.2 + 2.1 targeted + architecture/public API | `.venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/ tests/unit/test_app_jobs.py tests/integration/test_app_jobs_processes.py tests/unit/test_app_revision.py tests/unit/test_app_local_store.py tests/integration/test_app_store_processes.py tests/unit/test_codegraph_local.py tests/unit/test_cli_local_startup.py tests/unit/test_core_settings.py` | **140 passed** (before two lint-only test edits); `test_app_jobs.py` re-run after them: 43 passed |
| Concurrency/process tests, repeated 5× | `pytest … -k "process or concurrent or thread or held"` | 5 × 5 passed |
| **Full regression** | `MIN_START_MB=1500 .venv/bin/python scripts/safe_pytest.py {plan,run,report} <scratch>/pf-run22` (guards unchanged: start ≥ 1,500 MB, kill < 450 MB) | **3,247 collected = 3,188 passed + 54 skipped + 5 failed; 0 errors, 0 not executed, 0 duplicates, 0 ran-but-not-collected.** 113 batches, all completed, none refused. The 5 failures are exactly the known environmental S2 (`tests/integration/test_ops_doctor.py`, no local Redis). **No new regression.** Host minimum MemAvailable 1,532 MB |

Accounting against 1.3 (3,154 / 3,095 passed): +48 (2.1) +45 (2.2) = 3,247 collected, 3,188
passed. Same 54 skips, same 5 S2 failures. No source or test file changed after the run started.

### Milestone 2.2 status: **complete**
| # | Acceptance criterion | Evidence |
|---|---|---|
| 1 | Deterministic key, sensitive to every input, config validated | ✅ key tests (6 parametrized changes, 6 invalid configs) |
| 2 | Reuse without calling the job; non-succeeded never reused; single execution under concurrency | ✅ unit (4 threads) + integration (3 processes) |
| 3 | Lifecycle, closed failure classes, `job-report` on every terminal state | ✅ |
| 4 | Cooperative cancellation; cancelled runs publish no results | ✅ (before start, mid-run from a thread, after the last checkpoint) |
| 5 | Wall-time, artifact-count and artifact-bytes budgets → PARTIAL; revision budget → typed error, no run | ✅ fake clock, no timing assertions |
| 6 | SIGKILL → INTERRUPTED and recomputed; internal errors don't leak messages | ✅ real subprocess; secret-string check |
| 7 | Provisional API, gates green, regression | ✅ API 0.3.0, ruff/mypy/architecture green, full regression with only S2 |

## Milestone 2.1 — RepositoryRevision + LocalRunStore

### Files
| File | Change |
|---|---|
| `patchfrog/app/__init__.py`, `patchfrog/app/revision.py`, `patchfrog/app/store.py` (new) | the revision identity and the local run store (design: `docs/architecture/LOCAL_STORE.md`) |
| `tests/unit/test_app_revision.py` (new, 23) | identity, scope, Git/non-Git, symlinks, FIFOs, secrets, budgets, consistency retry/abort, repo-configured commands not executed, 3,000-file determinism |
| `tests/unit/test_app_local_store.py` (new, 22) | round trip, dedup, 64 MiB streaming within an 8 MiB `tracemalloc` bound, limits, corruption, schema, lifecycle, idempotency, path/symlink safety, retention, empty-env local run |
| `tests/integration/test_app_store_processes.py` (new, 3) | real SIGKILL during a run and during a write; 4 writers + 2 readers |
| `tests/architecture/rules.py` | `patchfrog.app` added as an R1 core root |
| `tests/architecture/public_api.py`, `docs/architecture/PUBLIC_API.md` | API 0.2.0: +9 **provisional** names (45 in total: 18 stable / 24 provisional / 3 deprecated) |
| `scripts/bench_local_store.py` (new) | bounded benchmark (developer tool) |
| Docs | `LOCAL_STORE.md` (new), ADR-002 implementation note, `BOUNDARIES.md` (2.1 measurement, R1 roots), `OVERVIEW.md` (RepositoryRevision), `TEST_STRATEGY.md` (benchmark rule), `ROADMAP.md` (2.1 definition and status), `CLAUDE.md` (core list, key docs) |

**No existing production module was modified** (checked by file timestamps; the import graph has
no edge into `patchfrog.app`).

### Findings during implementation
- **Real bug found by the concurrency test and fixed:** `RunHandle.finish("succeeded")` accepted
  a plain string (a `StrEnum` compares equal), but stored it unnormalized, which broke
  serialization. It is now normalized with `RunState(...)`, and unknown values raise
  `RunStateError` (test added).
- **Threat confirmed:** on this host, plain `git status` executed a repository-configured
  `core.fsmonitor` command *and* a clean filter. `capture_revision` on the same repository
  executed neither. That's why git is used only for listing.
- The stat-counting unit for excluded subtrees in directory mode was clarified (a pruned
  directory counts once).

### Tests executed (final file versions)
| What | Result |
|---|---|
| New 2.1 tests (unit + process) + `tests/architecture/` + `test_codegraph_local.py` + `test_cli_local_startup.py` + `test_core_settings.py` | **95 passed** |
| `tests/integration/test_app_store_processes.py`, repeated 5× (flakiness check) | 5 × 3 passed |
| `ruff check .` | **pass** |
| `mypy . --strict` | **pass** (851 files) |
| Benchmark (`scripts/bench_local_store.py`) | recorded in `LOCAL_STORE.md` §5 |
| Full regression, attempt 1 (implementation session) | **not executed**: memory never reached the 1,500 MB start threshold; 0 tests ran; not restarted, by rule |

### Verification session (2026-11-22, same working tree, no code changed)
| What | Command | Result |
|---|---|---|
| Ruff | `.venv/bin/ruff check .` | **pass** |
| mypy strict | `.venv/bin/mypy . --strict` | **pass** (851 source files) |
| Architecture + 2.1 targeted | `.venv/bin/python -m pytest -q -p no:cacheprovider tests/architecture/ tests/unit/test_app_revision.py tests/unit/test_app_local_store.py tests/integration/test_app_store_processes.py tests/unit/test_codegraph_local.py tests/unit/test_cli_local_startup.py tests/unit/test_core_settings.py` | **95 passed**, 0 failed, 0 skipped |
| Full regression, attempt 2 | `MIN_START_MB=1500 .venv/bin/python scripts/safe_pytest.py {plan,run,report} <scratch>/pf-run` (guards active: start ≥ 1,500 MB, kill < 450 MB) | **partial.** Plan: 3,202 collected, 111 batches (matches the expected 3,154 + 48). Batches 0–35 ran: **572 passed, 5 failed, 24 skipped, 0 errors, 0 duplicates, 0 ran-but-not-collected.** The 5 failures are exactly the known environmental S2 (`test_ops_doctor.py`, no local Redis). Batch 36 was refused by the guard (MemAvailable 1,076 MB < 1,500 MB). **2,601 tests (batches 36–110) not executed.** Peak pytest tree RSS 291 MB; host minimum seen 754 MB during batch 34 |
| Resume attempt | waited up to 600 s for MemAvailable ≥ 1,500 MB sustained 30 s, then `run --start 36` | **not resumed**: the condition was never met (host minimum 177 MB, 1,093 MB at the end). The guard was not bypassed or lowered |

Coverage of 2.1-specific code by the partial run: the architecture/public-API tests (batches 0–1)
and `test_app_store_processes.py` (batch 6) ran inside the full plan. `test_app_local_store.py`
(batch 79) and `test_app_revision.py` (batch 80) did not, but passed in the targeted run above.

**Remaining verification scope:** batches 36–110 of the plan (2,601 tests, mostly the frozen
review/compatibility lines and remaining unit tests). Command on a host with ≥ 1.5 GB stable free
memory: `MIN_START_MB=1500 .venv/bin/python scripts/safe_pytest.py run <dir> --start 36` against
the same plan (or a fresh `plan` + full `run`), then `report`. Expected: only the 5 S2 failures.

**Production-module check:** every modified existing production module (`patchfrog/cli.py`,
`config/settings.py`, `review/providers/openai_provider.py`, `upstream/{code_graph,workspace,blast_radius}.py`)
was last changed at 03:47–05:28 (Phases 1.1–1.3); the first 2.1 file dates from 14:51. No module
outside `patchfrog/app/` imports `patchfrog.app` (grep over `patchfrog/` and `apps/`). Milestone
2.1 therefore modified no existing production module.

### Milestone 2.1 status: **CONDITIONALLY ACCEPTED — Full regression deferred**

Owner decision (2026-11-22, D7): a **one-time, conditional** full-regression verification exception,
granted **for Milestone 2.1 only** because of documented host memory pressure. Recorded facts:
- implementation acceptance criteria 1–6 satisfied;
- `ruff check .` and `mypy . --strict` passed;
- 95 targeted/architecture tests passed;
- 596 planned test cases executed: 572 passed, 24 skipped, 5 known Redis-dependent environmental
  failures (S2);
- **2,601 planned test cases remain unexecuted** (batches 36–110);
- no new regression observed in the executed tests;
- **the full regression is not complete, and the full-regression gate is NOT green.**

The missing coverage stays an open blocker, **B-2.1-REG** (see "Blockers"), that must be closed
before a public release or a Cloud deployment. The exception is not a precedent: later milestones
need their own verification.

| # | Acceptance criterion | Status / evidence |
|---|---|---|
| 1 | Content/scope identity | ✅ `test_app_revision.py` |
| 2 | Git and non-Git | ✅ `test_app_revision.py` |
| 3 | No symlink following, no repo code executed | ✅ `test_app_revision.py` (incl. fsmonitor/filter threat test) |
| 4 | Typed errors for unstable tree / budget | ✅ `test_app_revision.py` |
| 5 | Store atomicity, dedup, corruption, lifecycle, SIGKILL recovery, idempotency, prune, path safety | ✅ `test_app_local_store.py`, `test_app_store_processes.py` (5× repeated) |
| 6 | Bounded memory | ✅ `tracemalloc` test; benchmark in `LOCAL_STORE.md` §5 |
| 7 | Provisional API, R1 root, gates green, **full regression with no new failures** | ⚠️ API/R1/ruff/mypy/architecture ✅; full regression **partial** (596 of 3,202 executed, no new failures among them) |

**Update (2.2 session):** the deferred coverage was executed. The 2.2 full regression (see
"Milestone 2.2 → Tests executed") ran every planned test on the final tree, which contains all of
the 2.1 code and tests: 3,247 collected = 3,188 passed + 54 skipped + 5 failed (S2), 0 not
executed. It included all three 2.1 test files and every one of the 2,601 previously unexecuted
tests. **The exception is therefore no longer needed, and 2.1 is verified.** Caveat: the tree was
the 2.1 code plus the additive `LocalRunStore.request_lock` from 2.2, not the 2.1 snapshot byte for
byte. The D7 record is kept for history and is not a precedent.

## Milestone 1.3 — neutral code graph + public API contract

### Implementation
| File | Change |
|---|---|
| `patchfrog/codegraph/local_graph.py` (new) | the local, DB-free code graph, moved from `patchfrog/upstream/code_graph.py`. Byte-identical below the module docstring (verified with `diff`) |
| `patchfrog/codegraph/__init__.py` (new) | public names: `EMPTY_GRAPH`, `MAX_GRAPH_FILE_BYTES`, `LocalCodeGraph`, `SymbolRef`, `build_local_code_graph` |
| `patchfrog/upstream/code_graph.py` | now a deprecated re-export of the **same objects** |
| `patchfrog/upstream/workspace.py`, `patchfrog/upstream/blast_radius.py` | import from `patchfrog.codegraph` (one line each) |
| `tests/architecture/rules.py` | `patchfrog.codegraph` added to the R1 `CORE_ROOTS` |
| `tests/architecture/public_api.py` (new) | Public Engine API v0: 36 names (18 stable, 15 provisional, 3 deprecated), `PUBLIC_API_VERSION = "0.1.0"` |
| `tests/architecture/test_public_api_contract.py` (new, 4) | well-formedness, R1 coverage of core entries, one-subprocess import check (aliases identical, no server `Settings` built by library modules, no network/process at import), document↔code sync |
| `tests/architecture/test_cloud_import_contract.py` | **removed** (replaced per ADR-017) |
| `tests/architecture/dependency_baseline.json` | one justification reworded generically (no consumer internals). The baseline itself is unchanged |
| `tests/unit/test_codegraph_local.py` (new, 3) | empty-env import and build (no server/LLM modules, not even `patchfrog.upstream`), reproducible output, legacy re-export identity |
| `tests/unit/test_core_settings.py` | one docstring generalized (no consumer internals) |
| `scripts/scan_cloud_imports.py` | **removed**. It was tooling for a private consumer, which now owns its copy (ADR-017) |
| `.gitignore` | ignores `/private/` and the former manifest path |
| Docs | `docs/architecture/PUBLIC_API.md` (new), ADR-017 (new) and ADR-015 wording in `DECISIONS.md`, `BOUNDARIES.md` (§2 measurement, §3 rewritten generically, R1 roots), `MIGRATION_PLAN.md` (M4 done; check procedure), `TEST_STRATEGY.md`, `OVERVIEW.md`, `ci-health.md`, `ROADMAP.md`, `CLAUDE.md` (core list, public-API and private-data rules), this file |

**Local, untracked (not part of the public tree):** `private/cloud-handoff/` holds the consumer
manifest, the scanner, a ready-to-copy CI test and migration instructions for the private
repository. It is git-ignored, and the owner moves it (not done here; the private repo was not
modified).

### Measurements
- Import graph after 1.3: 500 modules, 3,103 import edges (3,074 module / 28 lazy / 1
  type_checking), 51 units, 301 unit edges. **Historical SCC unchanged: 29 units, 156 edges.**
  The exact delta versus 1.2: +`codegraph → {dependencies, domain, indexing, intelligence,
  parsing}`, +`upstream → codegraph`, −`upstream → {domain, intelligence, parsing}`.
- `patchfrog.codegraph` closure: 31 modules; third-party only `tree_sitter*`, `yaml`. In a fresh
  empty-env interpreter, importing and building a graph loads no sqlalchemy, celery, redis, LLM
  SDK, fastapi, persistence, review, server settings, github or upstream module.

### Private consumer compatibility check (ADR-017; run locally, results only)
- **Root cause of the previous session's `IndexError`:** a wrong invocation, not a defect. The
  manifest path was passed relative while the probe subprocess ran with a different working
  directory. The child exited with `FileNotFoundError`, stdout was empty, and the parent's
  `splitlines()[-1]` raised `IndexError`. Not the scanner, not the manifest, not import resolution,
  not a regression.
- Re-run with an absolute path against the final working tree: **every recorded production import
  of the private consumer resolves, with 0 failures and 0 network/process attempts at import.**
  Its full recorded surface (production + test code) also resolves with 0 failures. A fresh static
  scan of the consumer checkout matches the manifest exactly. Exact counts and references are kept
  in `private/cloud-handoff/` only.

### Tests executed (final file versions)
| What | Result |
|---|---|
| Targeted: `test_codegraph_local.py`, `tests/architecture/`, `test_core_settings.py`, `test_cli_local_startup.py`, `test_cli_lazy_worker_imports.py` | **50 passed** |
| Upstream and compatibility regression (34 files importing `upstream`/`codegraph`/`migration*`/`campaigns`/`watchers`/`dependencies`, run with `safe_pytest`, one batch per file) | **432 passed, 10 skipped, 0 failed** |
| Negative checks (manual): wrong alias target, missing name, a module that builds `Settings` at import | all detected |
| `ruff check .` | **pass** |
| `mypy . --strict` | **pass** (844 source files) |
| Full regression (`scripts/safe_pytest.py`, 107 sequential batches) | **3,154 collected = 3,095 passed + 5 failed + 54 skipped; 0 errors, 0 not executed, 0 duplicates.** The 5 failures are the known environmental S2 (`test_ops_doctor.py`, no local Redis). **No new regression** |

### Full regression details
- The first attempt stopped at batch 8 (`test_campaign_orchestration.py`): the runner's kill guard
  fired when host MemAvailable fell to 423 MB, while pytest's own process tree used 109 MB. That
  was host pressure from unrelated applications. Batches 0–7 had completed (47 passed, 1 skipped).
- Resumed from batch 8 only after MemAvailable stayed ≥ 1,500 MB for 30 s, with the guards still
  active. All remaining batches completed. Batch 8 was re-run whole and counted once.
- Max peak process-tree RSS 295 MB. Integrity: only `docs/PROGRESS.md` changed after the first
  batch started, and no test reads it. Every test ran against final file versions.
- Versus 1.2 (3,149 collected / 3,090 passed): −2 (the removed private-consumer contract test) +7
  (4 public API + 3 code graph) = 3,154 / 3,095. Same 5 environmental failures, same 54 skips.

## Milestone 1.3 status — complete

| # | Acceptance criterion | Evidence |
|---|---|---|
| 1 | Code graph migration works | `test_codegraph_local.py` (empty-env build, reproducibility); implementation byte-identical |
| 2 | Legacy imports compatible | same-object re-export tested twice; 432 upstream/compat tests pass |
| 3 | Architecture boundaries pass | `tests/architecture/` green; baseline unchanged; `codegraph` is an R1 root |
| 4 | Public API contract passes | 4/4 (36 names, 0 import-time side effects, doc in sync) |
| 5 | Private consumer compatibility verified | yes, locally (0 failures, 0 side effects; details private) |
| 6 | Ruff and mypy pass | ✅ / ✅ (844 files) |
| 7 | Full regression without new failures | ✅ (3,154 accounted for; only known S2) |
| 8 | PUBLIC_API.md and ADR-017 complete | ✅ |
| 9 | Docs match implementation | ✅ doc↔code sync test; measured (not estimated) graph numbers |

## Earlier milestones (summary)

- **1.1:** `CoreSettings`/`ServerSettings` split (env contract identical, pinned by test), lazy
  `apps.worker` imports in the CLI (all 56 `--help` paths and the DB-free commands work in an
  empty environment), fix for S1 (OpenAI `insufficient_quota` via structured error codes),
  `scripts/safe_pytest.py`. Verified: 3,121 collected = 3,062 passed + 54 skipped + 5 failed
  (environmental S2).
- **1.2:** `tests/architecture/` (R1–R4, shrink-only baseline: 1 SCC × 29 units, 156 edges; 3
  reviewed lazy `apps.*` sites; 14 in-memory mutation proofs). Verified: 3,149 collected = 3,090
  passed + 54 skipped + 5 failed (S2). No production code changed.
- **Memory investigation (1.1):** the suite peaks below 300 MB. Earlier stops were caused by low
  host memory headroom. Use `scripts/safe_pytest.py` on small hosts (`docs/ci-health.md`).

## Known open items (ROADMAP backlog)

- **S2:** 5 Redis-dependent `tests/integration/test_ops_doctor.py` tests fail locally without
  Redis (environmental; CI has Redis).
- **S3:** the CLI imports server/LLM libraries eagerly (~2.2 s, no side effects).
- **S5:** `_mcp_serve` never disposes its engine.
- **S7:** DB-backed commands print a raw pydantic error without server configuration.
- Previously committed text that mentions the private consumer's internals (two docstrings in
  `patchfrog/persistence/repositories/learning.py` and `patchfrog/learning_records/domain.py`,
  some `validation/*` summaries) was deliberately left unchanged: history isn't rewritten, and
  `validation/` is a historical record. Cleaning it up is an owner decision.

## Next actions
1. **Owner: controlled commit and first CI run** (see "Git commit-readiness" in Milestone 2.5):
   - a feature branch with the Phase 1–2.5 changes, `/private/` excluded (already ignored);
   - open a PR so `ci.yml` runs the full suite with real Postgres and Redis;
   - record the result here. A green run closes **B-2.4-REG** and **B-2.5-REG**.
2. **Owner:** move `private/cloud-handoff/` into the private repository (ADR-017); decide ADR-018
   (console script; recommended for Phase 6).
3. **Exact next implementation task (needs approval): Phase 3a, shared detection prerequisites**:
   - request-path reachability: a bounded BFS from each route's endpoint and dependencies over
     `LocalCodeGraph`, joined to the discovery model;
   - DB-access *call sites* on identified sessions (DETECTION_RULES "Shared prerequisites" 3–4),
     with a corpus;
   - still no rules (PF-SCALE-*) and no MCP.
   The detection-rule corpus and the rules themselves follow in 3b.

## Blockers
- **B-2.4-REG (open; blocks public release and Cloud deployment):** the 2.4 full regression was not
  executed (host memory; two guarded attempts). To be closed by the first CI run.
- **B-2.5-REG (open; blocks public release and Cloud deployment):** the 2.5 full regression was not
  executed (not attempted, by owner instruction). To be closed by the same CI run.
- B-2.1-REG and B-2.3-REG: closed.
- Environmental only: local full runs need ≥ 1.5 GB free memory (`scripts/safe_pytest.py`).

## Open questions / recorded assumptions

- Assumed: the first target users run FastAPI + SQLAlchemy 2.0 (checked at G1/G2).
- The private consumer's CI job (ADR-017) doesn't exist yet. Until the owner wires it, its
  compatibility is only verified by local runs like the one recorded above.
