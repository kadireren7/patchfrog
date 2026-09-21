# CI health and test-suite contracts

PatchFrog's default test path is deterministic: it uses fake/oracle providers,
recorded fixtures, local repositories, and bundled Semgrep rules. It does not
need provider credentials and must not make paid provider calls.

## Suite classification

| Suite | Contract | Main/CI expectation |
| --- | --- | --- |
| Deterministic local | Unit and SQLite-backed integration tests, fixture repositories, fake/oracle providers | Green |
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
