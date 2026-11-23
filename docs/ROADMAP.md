# PatchFrog Roadmap — Scalability & Performance Engineering

The authoritative forward plan. Status lives in [`PROGRESS.md`](PROGRESS.md), architecture in
[`architecture/`](architecture/), and the previous roadmap in
[`history/roadmap-2026-review-and-compatibility.md`](history/roadmap-2026-review-and-compatibility.md).

**Ordering rationale (owner-approved 2026-11-22, D2/ADR-016):** MCP integration moves *earlier*
(Phase 4, alongside remediation) because MCP is the distribution channel and only a thin adapter.
PatchFrog is first built for the owner's own real projects. **G1** (after Phase 3) is a
non-blocking feedback checkpoint on those projects. **G2** (after the first complete MCP-driven
analyze → optimize → verify workflow, end of Phase 5) is the blocking external validation gate
before outside users (Phases 6–7). "Architecture foundations" is kept deliberately small: just the
boundaries the new core needs. There is no repository-wide cycle cleanup (D1).

Every phase must leave the standard gates green (`TEST_STRATEGY.md` §4): ruff, `mypy --strict`,
`pytest -q`, the architecture tests in `tests/architecture/` (from milestone 1.2), contract snapshots (from Phase 2).

---

## Phase 0 — Audit and baseline ✅ (approved 2026-11-22, with conditions D1–D5)
- **Deliverables:** `CLAUDE.md`, `docs/architecture/*`, this roadmap, `PROGRESS.md`, baseline
  gate results.
- **Acceptance:** documents exist, and the baseline is recorded with the exact commands. The
  owner accepts or amends the ADRs.

## Phase 1 — Local-first foundations and dependency boundaries
- **Objective:** PatchFrog can start with no services, and the new code has enforced boundaries.
- **Milestones** (each independently testable and reversible):
  - **1.1 ✅ verified (2026-11-22)** Lazy Celery imports in the CLI (M2) + `CoreSettings`/`ServerSettings`
    split (M1) + regression tests + server-compatibility tests. The full suite was reconciled
    per node id: 3,121 collected = 3,062 passed + 54 skipped + 5 failed (known environmental S2),
    0 not executed. Downstream import surface verified.
  - **1.2 ✅ (2026-11-22)** Architecture boundary enforcement (M3) in `tests/architecture/`: R1
    core-closure purity (12 approved roots), R2/R3 no cycle growth with a shrink-only baseline
    (1 historical SCC × 29 units, 156 intra-SCC edges), R4 `apps.*` direction (3 reviewed lazy
    sites), 14 in-memory mutation proofs, and a downstream import contract with import-time
    side-effect detection (moved out of the public tree in 1.3, ADR-017). Test-only; no production code changed. Spec and update procedure:
    [`architecture/BOUNDARIES.md`](architecture/BOUNDARIES.md).
  - **1.3 ✅ (2026-11-22)** `patchfrog/codegraph/` with a re-export from `upstream/code_graph.py`
    (M4; implementation byte-identical; historical SCC unchanged). Plus the Public Engine API v0
    contract (`architecture/PUBLIC_API.md`, 36 names) replacing the private-consumer manifest
    (ADR-017). Full suite: 3,154 collected = 3,095 passed + 54 skipped + 5 failed (environmental S2),
    0 not executed.
  - **Phase 1 status:** ✅ complete; verified in CI with real Postgres and Redis (run 45092267919, 2026-11-23).
- **Scope / modules:** `config/core.py` (new), `cli.py` (lazy imports), `codegraph/` (new, with
  re-export from `upstream/code_graph.py`), `tests/architecture/`.
- **Tasks:** MIGRATION_PLAN M1–M4.
- **Dependencies:** Phase 0 approval (ADR-011, ADR-012).
- **Risks:** an import side effect hidden in a CLI path, so a command breaks when its imports
  become lazy. Mitigation: a test that invokes `--help` on every subcommand.
- **Automated tests:** `env -i …/python -m patchfrog.cli --help` and every `<cmd> --help` exit 0.
  The boundary tests pass against the committed baseline (1 SCC × 29 units, 156 intra-SCC edges;
  the earlier "31 cycles" wording counted 2-cycles, which are informational only). Existing
  `Settings` tests unchanged.
- **Acceptance criteria:** (1) the commands above pass in CI. (2) `import patchfrog.codegraph`
  loads no `sqlalchemy`, `celery`, `anthropic`, `openai` or `google.genai` modules (asserted via
  `sys.modules`). (3) The existing suite's pass/skip counts are unchanged versus the Phase 0
  baseline. (4) The Celery task count is still 9.
- **Deferred:** settings split for the review line; fixing existing cycles.

## Phase 2 — Shared core services and project model
- **Objective:** one application-service layer that the CLI (and later MCP) calls, producing
  immutable, revision-pinned run artifacts.
- **Scope / modules:** `patchfrog/app/{jobs,store,contracts}`, `patchfrog/scalability/{domain,project}`,
  `cli_scale.py`.
- **Tasks:** M5, M6. `RepositoryRevision` (commit SHA + dirty-tree hash), `LocalRunStore` (atomic
  writes, idempotency, interrupted-run recovery, prune), `JobRunner` (lifecycle, budgets,
  cooperative cancellation), project model (framework/version detection via
  `dependencies.manifests`, FastAPI route discovery, SQLAlchemy model index), JSON Schema export.
- **Milestones** (each independently testable and reversible):
  - **2.1** `RepositoryRevision` + `LocalRunStore` (`patchfrog/app/revision.py`, `patchfrog/app/store.py`;
    design in [`architecture/LOCAL_STORE.md`](architecture/LOCAL_STORE.md)). No JobRunner, rules,
    CLI command or MCP change. **Acceptance:**
    (1) identical content and scope give an identical `revision_id`, independent of traversal order
    and of the absolute root path; any change to an in-scope file's bytes, a file add/remove, or a
    scope change gives a different id. (2) Git and non-Git directories are both supported, with
    explicit, tested behaviour (tracked plus untracked-not-ignored files; `.gitignore` honoured;
    the commit SHA is provenance, not identity). (3) Symlinks are never followed and nothing
    outside the root is read; repository code is never executed (no `git status`/filters/hooks/fsmonitor).
    (4) An unstable tree (changed during capture) or a budget overrun raises a typed error and never
    returns a wrong identity. (5) Store: atomic publication (no reader ever sees a partial
    artifact or record), content-addressed dedup, corruption detected on read and by `verify()`,
    explicit run lifecycle, interrupted runs and abandoned temp files recovered after a SIGKILL
    (tested with real subprocesses), idempotency lookup, retention/prune that never deletes live data,
    path-traversal and symlink defences. (6) Memory bounded for large artifacts (streaming;
    measured with `tracemalloc`). (7) New provisional public API entries, R1 core root
    `patchfrog.app`, all gates green, full regression via `safe_pytest` with no new failures.
    **Status: CONDITIONALLY ACCEPTED — full regression deferred (owner-approved, 2026-11-22).**
    Criteria 1–6 met; ruff, `mypy --strict`, architecture and public-API tests green; 95
    targeted/architecture tests passed. The full-regression part of criterion 7 is **not green**:
    596 of 3,202 planned tests ran (572 passed + 24 skipped + 5 failed, the known environmental
    S2; no new regression among them), and 2,601 (batches 36–110) were refused by the memory
    guard. The owner granted a one-time verification exception **for 2.1 only**. The missing
    coverage is tracked as blocker **B-2.1-REG** (PROGRESS.md) and must be closed before any public
    release or Cloud deployment. The exception does not carry over to later milestones.
    **Update (2026-11-22, 2.2 session): verified.** The deferred coverage ran in the 2.2 full
    regression (3,247 collected = 3,188 passed + 54 skipped + 5 failed S2; 0 not executed), so
    B-2.1-REG is closed and the exception is no longer needed.
  - **2.2** In-process `JobRunner` on top of `RepositoryRevision` + `LocalRunStore`
    (`patchfrog/app/jobs.py`; design in [`architecture/LOCAL_STORE.md`](architecture/LOCAL_STORE.md) §3). No project model,
    rules, CLI command, MCP tool, queue, thread/process pool or server-mode wrapper. **Acceptance:**
    (1) deterministic `idempotency_key` = sha256 over (job protocol version, kind, `revision_id`,
    canonical config fingerprint, engine version, ruleset version); any change to one input gives a
    different key; non-JSON or oversized config is rejected. (2) A repeat request with the same key
    returns the stored SUCCEEDED run **without calling the job**; PARTIAL/FAILED/CANCELLED/INTERRUPTED
    runs are never reused; concurrent identical requests (threads and processes) execute the job
    once. (3) Explicit lifecycle on the store's states with a closed failure-class enum and a
    `job-report` artifact on every terminal state (failure class, partial reasons, budget usage).
    (4) Cooperative cancellation via a thread-safe token checked at checkpoints: a cancelled run is
    CANCELLED and never publishes the job's artifacts as a result. (5) Budgets: wall time
    (cooperative stop at the next checkpoint → PARTIAL with reason), artifact count and total
    artifact bytes (→ PARTIAL), revision file/byte budgets (→ typed `budget_exhausted` error, no
    run); never a silent truncation. (6) A run whose process is SIGKILLed is recovered as
    INTERRUPTED by the next runner (real subprocess test); job exceptions become FAILED with a
    typed class and no leaked exception message for internal errors. (7) New provisional public
    API entries, all gates green, targeted tests; broader regression with `safe_pytest` only when
    host memory permits (the 2.1 exception does **not** apply to 2.2).
    **Status: ✅ complete (2026-11-22).** Criteria 1–7 met: 45 new tests (43 unit + 2 real-process),
    API 0.3.0 (+10 provisional), ruff and `mypy --strict` green, architecture unchanged (SCC 29/156),
    full regression via `safe_pytest` with 0 not executed and only the known S2 failures.
  - **2.3** Application discovery (project model, part 1): framework/version evidence from Python
    manifests and lockfiles, and static FastAPI route discovery (apps, routers, `include_router`
    prefix composition, aliases and imports, sync/async endpoints, `Depends` relationships), as a
    `JobRunner` job producing an immutable, revision-bound `discovery.json`
    (`patchfrog/scalability/project/`, `patchfrog/app/discovery.py`; spec
    [`architecture/DISCOVERY.md`](architecture/DISCOVERY.md)). No rules, SQLAlchemy model index,
    CLI or MCP. **Acceptance:** (1) a version is reported as resolved only from agreeing lockfile
    evidence, never from a range or pin. (2) Route discovery recall and precision = 100% on ≥ 12
    fixture apps with independently written expectations, including negative controls and
    unresolved/dynamic/unsupported cases. (3) Unresolvable constructs become typed `Unresolved`
    entries and never routes; `coverage.complete` is false whenever anything is unresolved,
    skipped or limited. (4) No analysed code is imported or executed, symlinks are not followed,
    and every read is verified against the revision. (5) Every bound (files, bytes, resolution,
    include and dependency depth, scope nesting, result counts, wall time) is explicit and tested.
    (6) It runs under the existing JobRunner with idempotency, cancellation and byte-reproducible
    artifacts. (7) The synthetic ~300-file layered fixture is analysed (trend target < 10 s,
    recorded, no gate). (8) Gates green; full regression via `safe_pytest` when host memory
    permits.
    **Status (2026-11-22): implementation complete, criteria 1–7 met; criterion 8 partial.** 14
    corpus cases: routes 36/36 (P = R = 1.00), dependencies 12/12; 7 of 7 evaluator mutations
    detected; 300-file fixture 0.32 s. That is fixture agreement only, not real-world accuracy
    (G1/G2 measure that). Ruff, `mypy --strict` and architecture tests green. The full regression
    ran 2,104 of 3,292 tests (only the known S2 failures); 1,188 were refused by the memory guard
    and were tracked as B-2.3-REG. No exception was granted. **Update (2.4 session): verified.** The
    resumed run completed (3,292 collected = 3,233 passed + 54 skipped + 5 S2, 0 not executed), so
    B-2.3-REG is closed. Details: `PROGRESS.md`.
  - **2.4** SQLAlchemy/SQLModel structure discovery (project model, part 2): declarative bases,
    models, tables, columns, primary keys, relationships and their loading configuration,
    inheritance, engines, session factories, sessions, session providers, and route → session
    links. Added as the `database` section of the same discovery job (schema 2, ruleset
    `fastapi.1+sqlalchemy.1`; spec [`architecture/DISCOVERY.md`](architecture/DISCOVERY.md) §9).
    No rules, no DB-access sites inside endpoints (Phase 3a), no CLI, no MCP. **Acceptance**
    (defined at the start of 2.4 from the owner's brief):
    (1) explicit, inferred, inherited, default and unknown facts are distinguished and never
    guessed;
    (2) no model code is imported or executed, no migration runs, no database is contacted, and
    only the scheme of a literal URL is kept;
    (3) dynamic, unsupported and unresolvable constructs become typed `DbUnresolved` entries and
    make the model incomplete;
    (4) an independent corpus of ≥ 12 cases (positive, negative, ambiguous, unsupported) has
    precision and recall = 100% for the supported patterns;
    (5) determinism, large file counts, bounded memory, cancellation, revision changes during
    analysis, malformed files, symlinks, duplicate names and import ambiguity are tested;
    (6) it runs under the existing JobRunner (idempotency, budgets, reproducible artifacts) with
    version-1 artifacts still readable;
    (7) R1 holds, there are no new cycles, and only necessary provisional API is added;
    (8) gates green, and a full regression via `safe_pytest` when host memory permits.
    **Status (2026-11-23): implementation complete, criteria 1–7 met; criterion 8 partial.**
    13-case corpus: all 7 supported categories P = R = 1.00 (models 20, columns 34, relationships
    13, objects 7, options 8, providers 6, route → session links 7); 10 of 10 evaluator mutations
    detected; real-world spot check: 45 of 45 models in this repository's persistence layer. That
    is fixture agreement only, not real-world accuracy. Ruff, `mypy --strict`, architecture and
    244 targeted tests are green. The **full regression was not executed** (host memory below the
    guard threshold in two attempts) and was tracked as B-2.4-REG. **Update (2026-11-23): verified in
    CI** (run 45092267919: 3,371 passed, 6 by-design skips, 0 failed) and B-2.4-REG is closed.
  - **2.5** Public CLI and discovery schema: `python -m patchfrog scale analyze|show|schema`
    (`patchfrog/cli_scale.py`, a thin adapter over `run_discovery`), and a versioned JSON Schema
    of `ApplicationModel` generated from the typed model (`docs/schemas/`). User docs:
    [`scale-cli.md`](scale-cli.md). **Acceptance** (defined at the start of 2.5 from the owner's
    brief):
    (1) `analyze` is local-only, reuses run_discovery/JobRunner/LocalRunStore, reports
    frameworks, routes, models, relationships, providers, unresolved references and completeness,
    distinguishes succeeded from partial, prints the run id, uses documented exit codes, and
    executes nothing from the repository;
    (2) `show` validates run ids, rejects corrupt, foreign-kind and unsupported artifacts, offers
    JSON, handles missing, interrupted, partial and failed runs, and never reports an incomplete
    discovery as complete;
    (3) the schema has one source, is deterministic, has explicit required/nullable/enum
    semantics, keeps version-1 compatibility, rejects future versions, and has snapshot and
    round-trip tests;
    (4) end-to-end tests run through the real entry point;
    (5) startup, first and repeated analysis, peak memory and artifact size are measured;
    (6) gates green and docs updated;
    (7) full regression: a CI responsibility.
    **Status (2026-11-23): implementation complete, criteria 1–6 met; criterion 7 not executed**
    (B-2.5-REG). **Update (2026-11-23): verified in CI** (run 45092267919) and B-2.5-REG is closed. 10 e2e + 38 schema tests; startup 0.15 s (fast
    path) versus 3.5 s (full CLI); this repository analysed in 3.3 s, reused in 0.34 s. Details:
    `PROGRESS.md`.
  - **Phase 2 status: ✅ complete and verified in CI** (2026-11-23, draft PR #69, not merged).
    Every planned deliverable is implemented. Route discovery precision and recall on the fixture
    set: 100%. The ~300-file fixture: 0.32 s. Needs no DB, network or environment variable.
- **Dependencies:** Phase 1.
- **Risks:** over-designing the job model. Mitigation: in-process only, one implementation, no
  plugin system.
- **Deliverables:** `patchfrog scale analyze <path>` prints the route table, frameworks and
  coverage (no findings yet). `patchfrog scale show <run_id>`.
- **Automated tests:** idempotency (same revision returns the same `run_id` and no recompute);
  dirty tree gives a different key; kill during a run, then the next open marks it `interrupted`;
  budget exhaustion gives `partial` with a reason; route discovery on ≥ 12 fixture apps (routers,
  prefixes, `Depends`, sync/async); schema snapshots.
- **Acceptance criteria:** route discovery recall = 100% and precision = 100% on the fixture set.
  A synthetic ~300-file layered FastAPI fixture (routers, services,
  repositories) analyses in < 10 s on CI hardware, with the result recorded (trend, no gate).
  Vendoring third-party repos is avoided because of licensing and size. No DB, network or env var needed.
- **Deferred:** `PostgresRunStore`, parallel parsing, the `include_router` cross-module prefix
  edge cases beyond the fixtures.

## Phase 3 — Scalability detection MVP
- **Objective:** three reliable rules with evidence and traces (DETECTION_RULES.md).
- **Scope / modules:** `patchfrog/scalability/{python_semantics,reachability,db_sites,rules/,remediation/}`,
  `tests/fixtures/scalability/cases/`.
- **Tasks:** 3a shared prerequisites (semantic pass, DB-site identification, request-path
  reachability). 3b **corpus first**, then PF-SCALE-001, -002, -003. 3c diff mode
  (`--diff-base REF`: findings new in head, using `diff/`).
- **Dependencies:** Phase 2.
- **Risks:** false positives from receiver ambiguity. Mitigation: no finding without an identified
  receiver, plus explicit confidence reasons. Python `ast` grammar version (ADR-004).
- **Automated tests:** corpus ≥ 8 positive / ≥ 8 negative / ≥ 2 unsupported per rule; trace
  correctness assertions (exact file:line path); budget and syntax-error robustness; diff mode on
  multi-commit fixtures.
- **Acceptance criteria:** per rule, corpus precision ≥ 0.90 and recall ≥ 0.80. **0 high-confidence
  findings on clean cases.** Every finding has ≥ 1 evidence item, a trace to an entrypoint (or
  `include_non_request`), assumptions and limitations. Determinism: two runs give byte-identical
  `findings.json`.
- **Deferred:** pool misconfiguration, concurrency rules, raw SQL, non-FastAPI frameworks.

### Checkpoint G1 — Early feedback on our own projects (non-blocking, ADR-016)
Run Phase 3 on ≥ 2 of the owner's own real FastAPI/SQLAlchemy projects at pinned SHAs. Triage
**every** finding by hand as TP/FP/unclear, with a one-line reason. Record precision for
high/medium findings per rule and every false-positive pattern in
`validation/scalability/g1-summary.md`. **Outcome:** a rule-fix backlog. Nothing is blocked, but
any rule with high-confidence precision < 0.7 on our own projects gets its confidence reduced
or a fix *before* Phase 4 exposes it through MCP.

## Phase 4 — MCP tools, remediation guidance, safe agent handoff
- **Objective:** coding agents can analyse, inspect and receive actionable, evidence-tied
  remediation, with no execution yet.
- **Scope / modules:** `patchfrog/mcp/scalability_tools.py` (new), `patchfrog/app/` use cases,
  `patchfrog/scalability/remediation/`.
- **Tasks:** M8. The tools `scalability_analyze/get_run/list_findings/get_finding` (MCP.md).
  Progress and cancellation. Typed error enum. Root confinement. Remediation templates per rule
  (pattern, before/after snippet using the finding's real symbols, risks, and "when *not* to apply").
- **Dependencies:** Phase 3.
- **Risks:** MCP SDK changes (pinned `mcp<2`). Tool-output size. Mitigation: 64 KiB cap with cursors.
- **Automated tests:** an in-process MCP client round trip for every tool; cancel mid-analysis
  gives `cancelled` and no succeeded artifact; path traversal and symlink escape rejected; error
  codes are a closed enum (snapshot); the review tools still work when `DATABASE_URL` is set.
- **Acceptance criteria:** a scripted agent session (fixture repo) goes analyze → get_finding →
  applies the template fix (in the test) → re-analyze shows the finding gone, with no PatchFrog
  file writes outside the cache dir (asserted with a filesystem snapshot). Claude Code connects via
  `patchfrog mcp serve` (manual check, recorded).
- **Deferred:** remote transport, any tool that writes files.

## Phase 5 — Verification and benchmark infrastructure
- **Objective:** measured confirmation of findings and fixes (VERIFICATION.md).
- **Scope / modules:** `patchfrog/execution/` (extracted, M9), `patchfrog/perf/` (M10), the
  `scalability_plan_verification/run_verification/compare` tools, and the CLI `scale verify|compare`.
- **Tasks:** user-level execution authorization (ADR-008); plan + `plan_hash`; pytest plugin with
  the `query_count` and `loop_lag` probes; seeding harness; statistics (warm-up, repeats,
  bootstrap CI, CV guard); environment fingerprint; ephemeral Postgres via the docker CLI (opt-in);
  optional k6 adapter (loopback only).
- **Dependencies:** Phase 4. G1 recorded (non-blocking).
- **Risks:** measurement noise; DB setup friction; the security of running project code.
  Mitigation: deterministic probes first, explicit `inconclusive`, refusing non-loopback targets.
- **Automated tests:** N+1 fixture gives slope ≥ 0.9 before and ≈ 0 after (deterministic, SQLite +
  Postgres in CI); blocking fixture gives a loop-lag delta; the statistics module on synthetic
  samples (improved/regressed/inconclusive/unstable); authorization matrix (repo config cannot
  enable; a missing hash is refused; a non-loopback URL is refused); env allowlist.
- **Acceptance criteria:** for every corpus positive with a runnable fixture, verify gives
  `reproduced` and the fix gives `verified_fixed` with ≥ 95% agreement over 20 repeated CI runs. 0
  verification runs without an authorization record. Reports separate predicted from measured
  effects and never extrapolate capacity.
- **Deferred:** profiler integration (py-spy/pyinstrument) beyond optional detection; hosted
  sandbox execution.

### Gate G2 — External validation (blocking for Phases 6–7, ADR-016)
**Precondition:** one complete MCP-driven workflow (analyze → get_finding → agent applies the fix
with approval → plan/run verification → compare → report) has worked end to end on at least one
real project. **Criteria:** (1) ≥ 10 external public FastAPI repos at pinned SHAs analysed and
triaged; (2) real-world precision ≥ 0.7 for high/medium findings, per rule; (3) ≥ 5 external
developers who use coding agents ran the workflow, and ≥ 3 would use it again; (4) ≥ 1
`verified_fixed` result in a repo the PatchFrog author doesn't own; (5) no open critical security
issue. Record the results in `validation/scalability/g2-summary.md`. Failing G2 means revising
the rules or the workflow before any public packaging or beta.

## Phase 6 — Distribution, packaging, client compatibility
- **Objective:** a one-command install for agent users.
- **Dependencies:** G2 passed. The ADR-015 Cloud compatibility check is done for the extras change (D5).
- **Tasks:** M11 extras split (slim base: tree-sitter, pydantic, structlog, mcp, pyyaml, httpx);
  `uvx patchfrog mcp serve`; tested config snippets for Claude Code, Codex and Cursor; CI
  compatibility matrix (TEST_STRATEGY §1); M12 license metadata (after ADR-013).
- **Acceptance criteria:** a clean venv `pip install patchfrog` (base, no extras) pulls in no
  semgrep, celery, asyncpg or LLM SDKs and installs in ≤ 60 MB, with the measured size and time
  recorded; `uvx` cold start → first analysis on a fixture in
  < 60 s; Docker images contain the same packages as before (diffed `pip freeze`); the Cloud pin is
  updated in a coordinated change.
- **Deferred:** a non-Python distribution (npm shim), or splitting the repository (ADR-007 criteria).

## Phase 7 — Open-source beta, documentation, external validation
- **Prerequisite:** ADR-013 executed (Apache-2.0, per `docs/legal/APACHE_2_0_MIGRATION.md`) with
  explicit owner approval.
- **Tasks:** user docs (quickstart for agents, rule reference, verification guide, limitations);
  external validation on ≥ 20 repos; feedback channel; a public rule-precision report; README
  repositioning (honest: suspected vs. verified).
- **Acceptance criteria:** ≥ 20 external users have run the tool (opt-in count or reported);
  real-world precision is published per rule; no critical security issues are open; all
  user-facing claims are traceable to tests or validation records.
- **Deferred:** new frameworks (Django, Flask) and new languages (TypeScript/Node is the likely
  next), decided from feedback.

## Phase 8 — Cloud-readiness assessment (assessment, not implementation)
- **Tasks:** `PostgresRunStore` design; Celery wrapping of `JobRunner` jobs; hosted sandbox
  requirements (network-isolated DB inside the sandbox); remote MCP (Streamable HTTP + OAuth)
  boundary; tenant-isolation review; a decision on whether frozen review/compat lines are retired,
  kept, or offered in Cloud only.
- **Deliverable:** an assessment document plus ADRs. Implementation belongs to `patchfrog-cloud`.

---

## Stabilization backlog (maintenance on existing lines; schedule alongside phases)

Every item needs a regression test first. None of them is a feature change for the frozen lines.

| ID | Item | Evidence | Priority |
|---|---|---|---|
| S1 ✅ | OpenAI `insufficient_quota` misclassified as transient. **Fixed 2026-11-22** in the OpenAI adapter only (`_has_insufficient_quota_code`: `exc.code`/`exc.type`/`exc.body`, also when still wrapped, exact match). The message fallback is kept. Anthropic/Gemini unchanged: neither has this structured code, and Gemini's 429/RESOURCE_EXHAUSTED is ambiguous by design | 7 regression tests through the real SDK error mapping; 4 of them fail on the old adapter | done |
| S2 | `tests/integration/test_ops_doctor.py` (5 tests) require a live Redis and fail instead of skipping locally. Make them hermetic (fake Redis ping) or use an explicit capability skip, **while keeping them enforced in CI** (the CI contract in `docs/ci-health.md` is unchanged) | Phase 0/1 baseline runs | medium: local signal quality |
| S3 | CLI cold import ~2.2 s (everything imported eagerly; 3.5 s measured for `python -m patchfrog.cli scale --help` on a loaded host, 2026-11-23). This also slows `mcp serve` startup. Lazy subcommand modules. **Mitigated for `scale` only** (2.5): `python -m patchfrog scale` skips the full CLI (0.15 s) | `python -X importtime -c "import patchfrog.cli"` | medium: before Phase 4 MCP UX |
| S4 | `pyproject.toml` `license = {text=…}` deprecated, setuptools deadline 2027-02-18 | pip build warning | tied to ADR-013 execution |
| S5 | `_mcp_serve` creates an engine and never disposes it | `patchfrog/cli.py` `_mcp_serve` | low |
| S6 | Local runs of the full suite can be stopped by host memory pressure on small machines (the suite itself peaks below 300 MB). Use `scripts/safe_pytest.py` locally; CI is unchanged | `docs/ci-health.md` → "Resource-constrained local runs" | done (tooling) |
| S7 | DB-backed CLI commands fail without server config with a raw pydantic traceback, not an actionable message | Phase 1.1 validation | low (UX) |
| S8 ✅ | Dependency drift: `kombu` 5.7.0 (released after the last green `main` CI, 2026-10-02) refuses `redis` < 6.1 at worker start (`VersionMismatch`), while `redis` is capped `<6.0` here and by `celery[redis]` 5.4. Every verifier-worker integration test errored in CI (18). **Fixed 2026-11-23** by declaring `kombu>=5.3.4,<5.7` (5.6.x accepts redis 5.x). ADR-015 impact assessment in `PROGRESS.md`. Moving to redis-py 6 is a separate, deliberate upgrade | CI run 45090246711; reproduced locally on `main`'s code | done |
