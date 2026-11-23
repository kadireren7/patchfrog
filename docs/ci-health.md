# CI health and test-suite contracts

PatchFrog's default test path is deterministic: it uses fake/oracle providers,
recorded fixtures, local repositories, and bundled Semgrep rules. It does not
need provider credentials and must not make paid provider calls.

## Suite classification

| Suite | Contract | Main/CI expectation |
| --- | --- | --- |
| Deterministic local | Unit and SQLite-backed integration tests, fixture repositories, fake/oracle providers | Green |
| Architecture (`tests/architecture/`) | Static AST boundary rules R1–R4 against a committed shrink-only baseline, plus one isolated subprocess that imports the Public Engine API v0 (`PUBLIC_API.md`) with sockets/process spawning disabled; no services, ~10 s | Green; a failure names the offending import and the update procedure (`docs/architecture/BOUNDARIES.md` §6) |
| External static tools | Ruff and Semgrep are runtime dependencies; cppcheck and clang-tidy are optional system capabilities | CI verifies Ruff/Semgrep before tests; missing optional tools are reported unsupported |
| Real PostgreSQL | Advisory-lock, concurrency, uniqueness, and cascade tests against the migrated `patchfrog` database on localhost | Local skip only when unavailable; CI sets `PATCHFROG_REQUIRE_POSTGRES=1`, so unavailable/misconfigured/unmigrated is a failure |
| Host isolation | Tests requiring functional `bwrap` plus `prlimit` | Explicit optional skip when the host lacks the capability |
| Distributed verifier | Tests requiring isolation plus a reachable local Redis worker round trip | Explicit optional skip when either prerequisite is absent |

Semgrep runs with telemetry and version checks disabled and uses isolated,
writable state. Its rules are repository-bundled, so analysis never downloads a
registry configuration. An installed-but-broken required analyzer is not
treated as a clean analysis result: discovery records it as unavailable and CI's
explicit version check fails the job.

## Reproduce locally

Run the deterministic suite and allow unavailable infrastructure cases to report
as skips:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy . --strict
```

Run the CI database contract after starting and migrating the declared service:

```bash
docker compose up -d postgres redis
DATABASE_URL=postgresql+asyncpg://patchfrog:patchfrog@localhost:5432/patchfrog alembic upgrade head
PATCHFROG_REQUIRE_POSTGRES=1 .venv/bin/pytest -q
```

`PATCHFROG_REQUIRE_POSTGRES=1` is deliberately a CI/infrastructure assertion:
it converts any connection, authentication, or missing-schema problem into a
test failure. Without it, the same database-only cases skip with an actionable
reason while the deterministic suite continues.

## Failure classification

- A deterministic assertion, unexpected exception, analyzer execution failure,
  or evaluation regression is a product/test regression and fails.
- An analyzer binary that is declared required but missing or unusable is an
  external-tool availability failure and fails CI during verification.
- A real-Postgres test without a compatible local database is an explicit local
  infrastructure skip; the same condition fails CI.
- Missing host sandbox or Redis capability for an optional integration is an
  explicit skip, never a clean-product assertion.
- There are no unconditional `xfail` entries masking known product failures.

Expected status for main is green: Ruff clean, strict mypy clean, deterministic
tests passing, required static analyzers verified, real-Postgres tests passing
in CI, and only capability-specific optional integrations skipped where their
documented host prerequisites do not exist.

Two non-failing upstream/runtime warnings may still be visible: an MCP SDK
Pydantic forward-reference warning during one governance-tool test, and a rare
Python asyncio subprocess-transport finalizer warning after a deliberately
killed analyzer process. Neither changes a test result or represents an ignored
assertion; they remain visible so dependency/runtime upgrades can remove them.

## Known local deviations from this contract (tracked, not product regressions)

Recorded 2026-11-22 (`docs/PROGRESS.md`, roadmap backlog S2). The CI behaviour described above is
**unchanged**. These items only affect local runs on hosts without the CI services.

| Tests | Local behaviour without Redis | Classification | Tracking |
|---|---|---|---|
| `tests/integration/test_ops_doctor.py`: `test_all_good_reports_pass`, `test_placeholder_webhook_secret_warns`, `test_gemini_provider_with_unset_model_now_correctly_defaults_to_gemini`, `test_exit_code_zero_for_pass_and_warn_only_one_for_fail`, `test_doctor_never_calls_an_llm_or_mutates_state` | **fail** (the doctor's real Redis ping reports `fail`), instead of the explicit skip this contract requires for optional infrastructure | environmental test-design gap | ROADMAP S2 |

Product regressions are tracked separately (ROADMAP "Stabilization backlog", e.g. S1) and are
never reclassified as environmental.

## Resource-constrained local runs

On small hosts, a single full `pytest` run can be stopped by **host** memory pressure. On
2026-11-22 a host with 7.2 GB RAM and ~1.5 GB available (most of it taken by unrelated desktop
applications) had Claude Code stop the run after 51 tests. Measurement afterwards showed the suite
itself is modest: collection peaks at ~220 MB RSS, and the slowest file
(`tests/integration/test_campaign_orchestration.py`, a sandboxed nested `pytest` per scenario) takes
~4.6 min at ~110 MB peak process-tree RSS. The suite is serial (no xdist). There are no
session-scoped fixtures, and only two module-scoped fixture pairs (the sandbox corpora).

For such hosts, use the batched runner (stdlib only, no new dependency). It runs one batch per
process, writes JUnit XML per batch, refuses to start below 1,000 MB available, kills the batch and
stops below 450 MB, and reconciles every collected node id:

```bash
.venv/bin/python scripts/safe_pytest.py plan   /tmp/pf-run      # collect + batch plan
.venv/bin/python scripts/safe_pytest.py run    /tmp/pf-run      # resumable: --start N
.venv/bin/python scripts/safe_pytest.py report /tmp/pf-run      # passed/failed/skipped/not executed
```

A run that stops early is reported as **incomplete**, with its "not executed" list. It is never
reported as a pass. CI is unaffected and keeps running the plain `pytest -q`.

## CI never prints key material

The workflow generates its disposable GitHub App key and webhook secret at run time (written to a file or
`$GITHUB_ENV`, and masked) instead of writing them in the job-level `env:` block, which GitHub prints --
resolved -- in every step header. `tests/unit/test_ci_workflow_hygiene.py` fails if a secret-looking literal
reappears in any `env:` block, if key or encoded-blob text is embedded in a workflow, or if a workflow prints
the environment or resolved Docker config. This guards the *configuration going forward*; it does not and
cannot erase anything already present in historical logs.

