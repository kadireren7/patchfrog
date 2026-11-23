# CLAUDE.md — PatchFrog

This is the starting point for every Claude Code session in this repository. It states the rules.
The architecture, the plan and the current status live in the documents linked below. Read them;
don't copy them in here.

## Mission

**PatchFrog — Make AI-generated code ready to scale.** PatchFrog is a local-first, MCP-native
scalability and performance engineering tool. It finds likely bottlenecks in application code
(first target: Python + FastAPI + SQLAlchemy), traces the path to each one, explains the cause with
evidence, proposes the simplest fix that works, and checks fixes with reproducible measurements.

Product rules that are never traded away:

- **Suspected ≠ verified.** Every finding carries `verification_status`
  (`suspected` | `reproduced` | `verified_fixed` | `inconclusive` | `refuted`). Static analysis on
  its own can only produce `suspected`.
- **No capacity claims.** Never state that code "supports N users". Only report measured deltas
  under stated assumptions (hardware, data volume, concurrency, repeat count).
- **Simplest architecture supported by evidence.** Never recommend microservices, Kubernetes,
  Redis, queues, caches or sharding because an app "might grow". A recommendation like that needs
  a measured bottleneck that simpler fixes cannot solve.
- **Deterministic first.** Baseline analysis never needs an AI API, network access or a hosted
  service.

## Repository responsibilities

The repo currently has three product lines. Know which one you are touching:

| Line | Status | Packages |
|---|---|---|
| **Scalability engine** (new, the focus) | being built, see `docs/ROADMAP.md` | `patchfrog/scalability/` and related (planned, see OVERVIEW) |
| **GitHub PR review** (Phases 1–9, J–Z) | maintenance only, consumed by private Cloud | `review`, `context`, `publishing`, `feedback`, `review_memory`, `*_intelligence`, … |
| **API/SDK compatibility** (M4–M11) | maintenance only | `dependencies`, `upstream`, `migration*`, `campaigns`, `watchers` |

Maintenance only (owner decision D4) means bug fixes, critical and security fixes, and
compatibility preservation, never new features. Nothing gets deleted. Self-hosted workflows
must keep working. The private `patchfrog-cloud` repo pins this engine. **Before changing
package extras, public interfaces, import paths, dependency declarations or persistence
behaviour, run the Cloud compatibility check (ADR-015, `docs/architecture/MIGRATION_PLAN.md`).**
Never edit Cloud code, never build Cloud features, and never create a `patchfrog-mcp` repo for
now (ADR-007).

## Architecture rules (details: `docs/architecture/OVERVIEW.md`, `DECISIONS.md`)

1. **Layering for new code:** `domain` (pure dataclasses/enums, no I/O) ← `engine` (pure analysis
   over a repository snapshot) ← `application services` (use cases, jobs, budgets) ← adapters
   (CLI, MCP, worker, API). Adapters hold no business logic. MCP and CLI call the same
   application service.
2. **New scalability packages must not import** `patchfrog.persistence`, `patchfrog.github`,
   `patchfrog.review`, `celery`, `redis`, `fastapi` (as a runtime dependency) or any LLM SDK.
   They may import only the approved core roots, whose closures are verified clean at module
   level: `parsing.registry`, `intelligence.{resolution,graph,tests}` (**not**
   `intelligence.queries`), `codegraph` (use it, not the deprecated `upstream.code_graph`),
   `app` (revision identity, local run store, in-process job runner and discovery use case, 2.1–2.3),
   `scalability` (the engine itself, 2.3),
   `dependencies.{files,manifests}`,
   `analysis.subprocess_sandbox`, `change_risk`, `diff`, `repository.git` and `config.core`.
   Enforced by `tests/architecture/` (rule R1, `docs/architecture/BOUNDARIES.md`). A new core
   package is added to `CORE_ROOTS` when it is created.
3. **Local mode needs no services.** No Postgres, Redis, GitHub App, network or AI API. Local code
   reads `patchfrog.config.core.CoreSettings` (no required fields) and **never** constructs the
   server `Settings`/`ServerSettings`, or imports `apps.worker` at module load. Run results go to
   immutable artifacts on disk (ADR-002). Postgres stays the backend for server mode.
4. **Every public contract is versioned**: MCP tool I/O, run artifacts, findings, rule IDs. A
   breaking change bumps `schema_version` and gets an ADR.
5. **Bounded everything:** file count, bytes, wall time, subprocess output, graph depth and result
   size all have explicit, configurable budgets. When a budget is exhausted the result is
   `partial` with a reason, never a silent truncation.
6. **Boundaries without a big-bang cleanup (D1):** don't refactor the historical 29-unit SCC
   wholesale. New code must not add a cycle, an edge inside the SCC, or a forbidden import
   (`tests/architecture/`, R2–R4). When debt is removed, shrink
   `tests/architecture/dependency_baseline.json` (BOUNDARIES.md §6). Never edit the baseline just to
   make a growth failure pass.
7. **Untrusted input:** analysed source is untrusted. Never `import` or execute it during static
   analysis (parse only). Subprocesses go through `analysis.subprocess_sandbox.run_sandboxed`
   (environment allowlist, timeout, output cap).

## Public API and private consumers (ADR-017)

- The engine's promised import surface is `docs/architecture/PUBLIC_API.md` together with
  `tests/architecture/public_api.py`. They must stay identical (a test enforces it). Changing a
  stable entry needs the ADR-015 check and a deprecation alias. Provisional entries need a
  changelog line.
- **Never put private-consumer data in this public repo:** no import manifests, file paths,
  commit SHAs, engine pins or implementation details of the hosted product. Such material goes
  only in the untracked `private/` directory (git-ignored) or in the private repository itself.
  Naming PatchFrog Cloud as a product is fine.

## Security rules

- Repository-controlled files (`.patchfrog.yml`, the code itself) **can never authorize**
  execution, network access, provider/model choice or publication. Authorization comes from the
  user: CLI flags, user-level config, or explicit per-call MCP approval (ADR-008).
- PatchFrog never runs load tests or benchmarks against non-local targets. No production load
  testing, ever, and no auto-triggering.
- MCP tools never write source files, commit, push or call GitHub. The coding agent applies
  changes after the user approves them.
- Never log, persist or echo secrets or environment values. Keep the allowlist environment for
  subprocesses.

## Coding standards

Python 3.12, `ruff check .` clean, `mypy . --strict` clean, frozen dataclasses for domain values,
`StrEnum` for wire enums, typed boundaries, no `Any` on public contracts. Prefer the stdlib (`ast`)
over new dependencies. **Adding a runtime dependency needs an ADR.** Follow existing module
docstring conventions. Comments explain *why* a constraint exists, not what the code does.

## Testing requirements (details: `docs/architecture/TEST_STRATEGY.md`)

- Default `pytest` is hermetic: no network, no paid API, no credentials. Use fake providers.
- Every detection rule ships with positive and negative (clean, false-positive guard) corpus
  cases, and the corpus precision gate in the roadmap must hold.
- Measurement code is tested on deterministic signals (query counts, call detection) before any
  timing assertion. Never write timing assertions with fixed thresholds in unit tests.
- Real-Postgres tests skip locally when the DB is missing. CI sets `PATCHFROG_REQUIRE_POSTGRES=1`.
- Report results in three groups: product regressions, environmental failures (listed in
  `docs/ci-health.md` → "Known local deviations") and skips. Never mix them up.
- Full-suite results need per-node-id accounting. On small hosts, use `scripts/safe_pytest.py`
  (`docs/ci-health.md`). Never restart a run that was stopped for resource reasons without a
  resource-safe plan.
- Local-first changes are verified in a fresh interpreter with an empty environment
  (`tests/unit/test_cli_local_startup.py` pattern). The suite's conftest fills in server env vars,
  so in-process tests can't prove local startup.

## Documentation requirements

One source of truth per subject. When a change alters behaviour, update the owning document in
the same change. Status lives in `docs/PROGRESS.md`, the plan in `docs/ROADMAP.md`, decisions in
`docs/architecture/DECISIONS.md`. `docs/history/` and `validation/*/latest-summary.md` are
historical records: don't rewrite them.

## Licensing

The current license is ELv2 (source-available), and the intended final license is Apache-2.0
(ADR-013). Don't change `LICENSE` without explicit owner approval. Don't call PatchFrog "open
source" in user-facing text yet. New runtime dependencies must be Apache-2.0-compatible
(`docs/legal/APACHE_2_0_MIGRATION.md` §2).

## Handling uncertainty

If an assumption can't be checked from code, tests or docs: write it down as an assumption,
choose the reversible option, and record it in `docs/PROGRESS.md` under "Open questions". When a
product or architecture choice is the owner's to make, document the alternatives plus a
recommendation in `DECISIONS.md` as `Proposed`, and don't implement it until it is `Accepted`.
Never present untested behaviour as working.

## Session workflow

**At the start of every session:**

1. Read `CLAUDE.md` (this file).
2. Read `docs/PROGRESS.md`.
3. Read the architecture documents relevant to the task (`docs/architecture/*.md`).
4. Check the actual repo state (`git status`, `git log -10`, branch).
5. Find the current phase in `docs/ROADMAP.md`.
6. Confirm the phase's dependencies and acceptance criteria before implementing anything.

**At the end of every implementation session:**

1. Update `docs/PROGRESS.md`.
2. Record which tasks were completed and which were not.
3. Record the tests actually run, with exact commands and pass/fail/skip counts.
4. Record blockers and any architectural decisions (add ADRs to `DECISIONS.md`).
5. Name the exact next task.
6. Keep `docs/ROADMAP.md` consistent with actual progress.

Never claim work is complete without code, tests or recorded evidence. Never silently drop an
architectural decision: supersede it with a new ADR. Don't rewrite completed modules without a
specific, documented reason. Commit or push only when the owner asks.

## Definition of Done (every task)

- [ ] Acceptance criteria from `docs/ROADMAP.md` met, with evidence
- [ ] `ruff check .`, `mypy . --strict` and `pytest -q` pass (or failures recorded as pre-existing)
- [ ] New behaviour covered by tests, including failure and budget-exhaustion paths
- [ ] `tests/architecture/` passes (baseline only shrinks), no new runtime dependency without an ADR
- [ ] Owning docs updated, and `docs/PROGRESS.md` updated with the next task

## Key documents

`docs/PROGRESS.md` · `docs/ROADMAP.md` · `docs/architecture/OVERVIEW.md` ·
`docs/architecture/CURRENT_STATE.md` · `docs/architecture/DECISIONS.md` ·
`docs/architecture/MIGRATION_PLAN.md` · `docs/architecture/SCALABILITY.md` ·
`docs/architecture/MCP.md` · `docs/architecture/BOUNDARIES.md` · `docs/architecture/PUBLIC_API.md` · `docs/architecture/LOCAL_STORE.md` · `docs/architecture/DISCOVERY.md` · `docs/architecture/DETECTION_RULES.md` ·
`docs/architecture/VERIFICATION.md` · `docs/architecture/TEST_STRATEGY.md` ·
`docs/product-boundary.md` (Cloud boundary) · `docs/ci-health.md` (suite contracts) ·
`docs/legal/APACHE_2_0_MIGRATION.md` (license audit)
