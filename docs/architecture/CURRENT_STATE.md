# Current State — Repository Audit (2026-11-22, `main` @ 9b9de85)

An audit based on the code itself, not on what the READMEs say. The evidence labels are:
**[code]** = read in source, **[test]** = covered by the automated suite, **[ran]** = executed
during this audit, **[doc]** = only claimed in documentation.

## 1. What PatchFrog is today

PatchFrog is a **self-hosted GitHub App backend** for AI-assisted PR review. A second product line
(API/SDK compatibility migration) has been added on top of it. It is **not** a local developer
tool, and it has **no scalability or performance analysis capability** at all. There are no
FastAPI or SQLAlchemy rules, and `FindingCategory.PERFORMANCE` exists only as a taxonomy value
(`patchfrog/analysis/domain.py:49`).

| Metric | Value | Source |
|---|---|---|
| Python source (patchfrog/ + apps/) | ~103k lines, 47 top-level packages | [ran] `find … \| wc -l` |
| Test code | ~64k lines, 323 test files, 2,873 test functions | [ran] |
| Alembic migrations | 38 (`0001` … `0038`) | [code] |
| Commits / authors | 54 commits, 2026-08-15 → 2026-10-02, a single author | [ran] `git log`, `git shortlog` |
| Product pivots so far | 2 (PR review → API compatibility in M4), the proposed one would be the 3rd | [doc] `docs/history/roadmap-2026-review-and-compatibility.md` |
| External users | none on record; that roadmap's last line plans "onboard five to ten real users" | [doc] |

Development has been very fast: ~167k lines in 7 weeks. The amount of code is far larger than the
amount of real-world validation. The quality benchmarks (`evaluation_baselines/phase8_baseline.md`:
P=R=F1=1.000) use the **`fake-oracle` provider, scripted from ground truth**. They measure
pipeline plumbing, not detection quality. The real-scenario corpus
(`validation/review_effectiveness/corpus/`) has 3 cases. The docs say this themselves
(`docs/history/roadmap-2026-…md`, "Test counts are not review quality").

## 2. Runtime topology today

```
GitHub webhook ─▶ apps/api (FastAPI) ─▶ Celery (Redis) ─▶ apps/worker tasks (9) ─▶ PostgreSQL
                                                     └─▶ apps/verifier (separate Celery app, bwrap sandbox)
CLI (patchfrog.cli, 2.7k lines) ──────────────▶ same services, same PostgreSQL
MCP stdio server (patchfrog/mcp/server.py) ───▶ same PostgreSQL, PR-review data only
```

**The local-first blocker [ran]:** `env -i python -m patchfrog.cli --help` exits 1 with a
pydantic `ValidationError` (DATABASE_URL, REDIS_URL, GITHUB_APP_ID, GITHUB_WEBHOOK_SECRET
required). Causes:

- `patchfrog/config/settings.py:31-45`: the single `Settings` makes DB, Redis and GitHub App
  fields mandatory.
- `patchfrog/cli.py:40-41`: the CLI imports `apps.worker.celery_app` at module load, and that
  calls `get_settings()` at import time (`apps/worker/celery_app.py:16`).
- Every CLI command builds an engine from `settings.database_url` (`patchfrog/cli.py:253+`,
  `cli_changes.py:163+`).

Today you can't run any PatchFrog command, the MCP server included, without a GitHub App identity
and a database URL.

> **Update 2026-11-22 (Phase 1.1):** resolved for `--help` and the DB-free commands (lazy Celery
> imports plus `CoreSettings`). DB-backed commands and the PR-review MCP tools still need server
> configuration by design. See `docs/PROGRESS.md`. The rest of this document is the Phase 0
> snapshot.

## 3. Dependency structure [ran]

An AST import scan of top-level packages found **31 bidirectional (2-cycle) package
dependencies**. The ones that matter most:

- `persistence` imports 20 domain packages (e.g. `persistence/repositories/ai_finding.py:10` →
  `review.domain`), while those packages import `persistence` back. The domain and ORM layers are
  interleaved, with no direction.
- `review` imports 20 packages: it is the hub of the review line.
- `config` → `review` (`settings.py:15-16`): configuration depends on a feature package.
- `ops/orchestrator.py:105` (a library) imports `apps.worker.tasks` (an application), and
  `cli.py` → `apps.worker`: inverted layering.

**DB-free, reusable primitives exist [code]:** `parsing` (→ `domain` only), `intelligence/graph.py`,
`intelligence/resolution.py`, `upstream/code_graph.py` (`build_local_code_graph`: the full
index→resolve→graph pipeline *without a database*), `dependencies/files.py` (safe repository
walker), `dependencies/manifests.py`, `change_risk` (→ `diff` only), `analysis/subprocess_sandbox.py`.

## 4. Persistence and concurrency semantics [code]

- SQLAlchemy 2.0 async, capped `<2.1` (`pyproject.toml`, aiosqlite StaticPool fixture issue).
- **Idempotency and concurrency depend on PostgreSQL `pg_advisory_xact_lock`** in 9+ places
  (`persistence/repositories/{analysis_run,review_run,review_generation,review_publication,context_bundle,repository_index}.py`,
  `migration/store.py:46`, `dependencies/registry.py:75`). They **silently do nothing on SQLite**
  (`if dialect != "postgresql": return`).
- Partial unique indexes declare both `postgresql_where` and `sqlite_where` in the models.
- Tests run on SQLite through `Base.metadata.create_all` (`tests/integration/conftest.py:27-32`),
  **not through Alembic**. The migration chain is only exercised on Postgres in CI.
- The migrations have ~120 `alter_column`/`drop_column`/`drop_constraint` operations, and
  `migrations/env.py` doesn't enable `render_as_batch`. **`alembic upgrade head` on SQLite would
  fail.** SQLite is therefore a test convenience, not a supported runtime backend.
- Engines are created per CLI command and per Celery task (`apps/worker/tasks/*.py`, via `asyncio.run`).

## 5. Component audit

Actions: **KEEP** (unchanged, reused as is) · **EXTEND** (reused, with new capability added) ·
**REFACTOR** (reused, but its boundaries must change) · **FREEZE** (maintenance only, not removed)
· **DEPRECATE** / **REMOVE** (none recommended now).

| # | Component | Purpose / capabilities | Coupling | Debt / risks | Reuse for scalability | Action |
|---|---|---|---|---|---|---|
| 1 | `parsing/` (1.1k) | tree-sitter Python/C/C++: symbols, imports, calls; `PARSER_VERSION` cache key [code][test] | → `domain` only | `ParsedCall` has only `callee_name`: no receiver, loop or await context (`domain/code.py:88-99`) | Symbol/call skeleton for call graphs. Rule semantics need a richer Python pass (ADR-004) | **KEEP** |
| 2 | `intelligence/` (0.8k) | Conservative call/import resolution, 5 tiers, fail-closed on ambiguity; graph edges [code][test] | `queries.py` → persistence; `graph.py`, `resolution.py` pure | Name-based, receiver types unknown, method calls resolved only via `self` | Interprocedural tracing (route → helper → DB call) | **KEEP** (pure parts) |
| 3 | `upstream/code_graph.py` | DB-free `LocalCodeGraph` (callers/callees/tests) [code][test] | pure primitives | Lives under the compat line's package | **Core asset** for local tracing | **EXTEND** (move to a neutral package in Phase 1, keeping a re-export) |
| 4 | `dependencies/` (2.7k) | Safe walker, manifests and lockfiles (pip/poetry/uv/npm/go), stdlib-`ast` SDK call scanning, registry [code][test] | `registry.py` → persistence | Registry is Postgres-locked | Framework/version detection (FastAPI, SQLAlchemy, drivers), safe file walking | **KEEP** (pure parts), registry FREEZE |
| 5 | `analysis/` (2.8k) | ruff, semgrep, cppcheck, clang-tidy adapters, dedupe, fingerprints, toolchain identity; `subprocess_sandbox.run_sandboxed` [code][test] | service → persistence + index | Service requires a persisted, matching index (`service.py:159`) | Sandbox + adapter protocol + fingerprinting pattern | **KEEP** sandbox/adapters; service FREEZE |
| 6 | `indexing/` (0.9k) | Inventory, incremental parse cache (DB) [code][test] | ↔ persistence, intelligence, repository | Cache is DB-backed | `inventory.is_test_path`, language detection | **KEEP** |
| 7 | `repository/` (0.6k) | git wrappers, snapshots (clone/checkout), file contents [code][test] | ↔ indexing | none major | Revision pinning for immutable evidence (commit SHA, dirty-tree hash) | **KEEP** |
| 8 | `change_risk/`, `diff/` (1.1k) | Pure diff parsing and PR change-risk tiers [code][test] | → diff only | none | Diff-scoped scalability analysis ("regression introduced by this change") | **KEEP** |
| 9 | `executable_verification/` (1.4k) + `apps/verifier` | bwrap + prlimit sandbox, pytest adapter, eligibility, request/result protocol, separate credential-minimal process [code][test; host-isolation tests skip without bwrap] | → review, change_intelligence, test_intelligence | `--unshare-all` means **no network, so no DB-backed benchmarks** inside the sandbox. Eligibility is tied to review hypotheses | Sandbox core and pytest adapter for verification (Phase 5) | **REFACTOR** (extract a review-independent execution core) |
| 10 | `fix_verification/`, `agent_handoff/` (1.7k) | Finding→handoff evidence bundle (`schema_version`), fix-attempt verification [code][test] | 12 packages incl. github, persistence, review | PR/GitHub-centric | Handoff *pattern* (bounded evidence, schema version, fail-closed IDs) | **FREEZE**, reuse the pattern |
| 11 | `mcp/` (0.5k) | FastMCP stdio server, 7 tools over PR-review data [code][test] | → persistence, review, github (via fix service) | Needs DB + GitHub-keyed `repository_full_name`. Errors are untyped dicts. No progress or cancellation. No tool versioning | Server bootstrap, read-mostly trust model, ID-scoping discipline | **EXTEND** (new scalability tool group on a shared application service; existing tools kept) |
| 12 | `review/` (9.9k) + providers | Multi-agent LLM reviewer, critic, budgets, model router; Anthropic/Gemini/OpenAI adapters [code][test] | hub (→20 packages) | Heavy, LLM-centric, base dependencies pull in 3 provider SDKs | Not needed for the baseline. Optional later as an explanation layer | **FREEZE** |
| 13 | `context/`, `review_memory/`, `publishing/`, `feedback/`, `telemetry/` (~10k) | PR context selection, incremental memory, GitHub review publishing, feedback polling, telemetry [code][test] | persistence + github | PR-review specific | Little reuse | **FREEZE** |
| 14 | J–R intelligence layers, `learning_records/`, `governance/`, `merge_readiness/` (~8k) | Deterministic PR evidence layers, policy, readiness [code][test] | ↔ review, persistence | Large surface, never validated with real users | Governance "tighten-only" precedence pattern | **FREEZE** |
| 15 | `migration*/`, `campaigns/`, `watchers/`, `upstream/` (rest) (~13k) | Contract diff, blast radius, migration patches, verification lab, PR dossiers, watchers [code][test] | persistence, github | M9 publication not wired into production pipeline [doc] | Blast-radius pattern, evidence-bundle and dossier patterns | **FREEZE** |
| 16 | `evaluation/` (4.4k) | Corpus loader, deterministic matcher, precision/recall metrics, regression gates, baselines [code][test] | → production packages (one-way) | AI numbers come from the oracle provider | **Corpus and metrics framework for rule precision gates** | **EXTEND** |
| 17 | `config/` | One `Settings` with mandatory server fields | → review | **Blocks local-first** (§2) | — | **REFACTOR** (split CoreSettings/ServerSettings) |
| 18 | `persistence/` (6.8k) + `migrations/` | 30 repositories, 23 model modules, 38 migrations | ↔ 20 packages | Postgres-only semantics (§4) | Server-mode store only | **KEEP** for server mode. New local store separate (ADR-002) |
| 19 | `apps/api`, `apps/worker`, Celery | Webhook ingress, 9 tasks, metrics | → everything | `asyncio.run` per task, no generic job model or cancellation | Server mode only | **FREEZE** (later: host the generic job runner) |
| 20 | `github/`, `services/` | App auth, REST client, installation sync, PR ingestion | → domain, persistence | — | Not needed locally | **FREEZE** |
| 21 | `ops/` | doctor, preflight, metrics, orchestrator | → apps.worker (inverted) | Layering violation | `doctor` pattern for a local `patchfrog doctor` | **REFACTOR** later (low priority) |
| 22 | `cli.py` (2.7k) + `cli_changes.py`, `cli_campaigns.py` | argparse, all commands | imports Celery app at load | Monolithic, eager imports | Entry point | **REFACTOR** (lazy imports, add a `scale` command group) |

## 6. CI/CD, security, licensing

- **CI** (`.github/workflows/ci.yml`): ruff, semgrep presence check, `mypy --strict`, Alembic on
  Postgres 16, head check, pytest with `PATCHFROG_REQUIRE_POSTGRES=1`, a 5-case oracle eval sanity
  run, Docker build and Celery task-count check. A workflow hygiene test stops secrets from
  entering `env:`. Solid. [code]
- **Security controls** [code][test]: subprocess env allowlist and output caps; bwrap filesystem
  confinement with prlimit (after an escape was found and fixed, per
  `validation/executable_verification/latest-summary.md`); secret-value-free dependency discovery;
  repo config can't choose provider, model or credentials; MCP ID-scoping returns identical
  not-found shapes.
- **Deployment**: `docker-compose.yml` (postgres, redis, api, worker, verifier) and
  `docker/Dockerfile` (api/worker targets). Self-hosted only.
- **License**: Elastic License 2.0 (`LICENSE`). Earlier history was Apache-2.0
  (`docs/legal/LICENSE-APACHE-2.0-HISTORICAL.txt`). ELv2 is **not OSI-approved**, so the proposed
  tagline "open-source" **can't be used truthfully** under the current license. Single copyright
  holder (`git shortlog`), no CLA, so relicensing is legally simple but a product decision
  (ADR-013). `pyproject.toml` uses the deprecated `license = {text=…}` table (setuptools deadline
  2027-02-18, seen in the pip warnings).
- **Private Cloud** (`kadireren7/patchfrog-cloud`) **already exists** (Milestones W/X, per
  `docs/product-boundary.md`) and pins this engine's review pipeline services. This is a live
  compatibility constraint on any refactor.

## 7. Documentation state

- 46 top-level docs, one per milestone. The old `docs/roadmap.md` called itself authoritative but
  was stale (it said Y/Z and M6/M7 were "under development, not merged", yet both are on `main`).
  `docs/architecture.md` marked M8 as "PARTLY EXISTS" while the roadmap said implemented.
  **Remedied in this session:** the roadmap moved to `docs/history/`, `architecture.md` was renamed
  `docs/compatibility-pipeline.md`, and the new sources of truth are in `docs/architecture/`,
  `docs/ROADMAP.md` and `docs/PROGRESS.md`.
- `repository-learning.md` and `repository-learnings.md` describe two different milestones (Y vs.
  O) under near-identical names: a confusion risk, left as is.
- `.patchfrog.yml` and some validation summaries refer to a "CLAUDE.md" section that was never
  tracked in git. The new root `CLAUDE.md` supersedes it, and the Cloud boundary lives in
  `docs/product-boundary.md`.

## 8. Implemented vs. tested vs. claimed

| Claim | Status |
|---|---|
| Static analysis, context, AI review, publishing, memory, feedback | implemented + unit/integration tested; live-provider runs recorded in `validation/production_e2e/` [doc] |
| "Precision/recall 1.000" | **oracle provider only**. Not evidence of real review quality |
| Executable verification isolation | tested where `bwrap`+`prlimit` exist; skipped otherwise |
| M9 migration PR publication in production | **not wired** into the webhook/worker pipeline [doc] |
| Learning personalization | implemented, **not wired** into live review scheduling [doc] |
| MCP server | implemented and tested, PR-review data only, needs a DB |
| Scalability/performance analysis | **does not exist** |
| Local-first operation | **does not work** (§2) [ran] |

Baseline gate results: see `docs/PROGRESS.md` → "Baseline (Phase 0)".
