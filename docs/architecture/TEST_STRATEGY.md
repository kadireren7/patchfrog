# Test Strategy

Existing contracts (`docs/ci-health.md`) stay in force: a hermetic default suite, no paid
provider calls, real-Postgres tests required in CI, optional skips for bwrap and Redis. This
document adds the layers the scalability line needs.

## 1. Layers

| Layer | What | Where | Gate |
|---|---|---|---|
| Unit | Pure functions: semantic pass, route discovery, rule predicates, statistics, idempotency keys, budget accounting | `tests/unit/scalability/`, `tests/unit/perf/` | every PR |
| Architecture | R1–R4 boundary rules (ADR-012, `BOUNDARIES.md`): core-closure purity, no cycle growth with a shrink-only baseline, `apps.*` direction; mutation proofs; analyzer self-tests; Public Engine API v0 contract (`PUBLIC_API.md`, ADR-017; private consumers verify their own usage in their own CI). R5 (`env -i … --help`, no import-time server side effects) lives in `tests/unit/test_cli_local_startup.py` | `tests/architecture/` (~10 s) | every PR |
| Contract | JSON Schema snapshots of MCP tool I/O and run artifacts. Additive change → minor bump, otherwise the test fails | `tests/unit/contracts/` | every PR |
| Rule corpus | Fixture mini-apps per rule: ≥ 8 positive, ≥ 8 negative (false-positive guards from DETECTION_RULES "Expected false positives"), ≥ 2 unsupported-version cases | `tests/fixtures/scalability/cases/<rule>/<case>/` + `case.yaml` (expected findings with rule, symbol, confidence floor) | every PR. Thresholds in §2 |
| Integration | CLI `scale analyze` end to end on fixtures; MCP stdio round trip using the `mcp` client SDK in-process; cancellation and budget exhaustion; restart recovery (kill mid-run, then `interrupted`) | `tests/integration/scalability/` | every PR |
| Verification | Probes against fixture FastAPI apps: query-count slope for N+1 before and after; loop-lag on an injected `time.sleep`; comparison verdict logic on synthetic samples (deterministic) | `tests/integration/perf/` (SQLite for query_count; Postgres-marked variants) | every PR (SQLite), CI (Postgres) |
| Compatibility | Matrix: Python 3.12/3.13 host; analysed code using FastAPI {0.100, latest}, SQLAlchemy {2.0 sync, 2.0 async}, SQLModel; MCP client smoke for Claude Code / Cursor / Codex configs (documented manual checklist plus a scripted stdio client) | CI matrix job (Phase 6) | release |
| External validation | ≥ 10 public FastAPI repos at pinned SHAs: findings triaged by hand as TP/FP/unclear, stored in `validation/scalability/` | manual + scripted | Phase 7 gate |
| Security | Path traversal outside the root; symlink escape; execution refused without authorization; repo `.patchfrog.yml` cannot enable execution; non-loopback target refused; env allowlist (no `DATABASE_URL`/`*_API_KEY` in the child env); output caps; malicious fixtures (huge files, deep nesting, syntax bombs) stay within budgets | `tests/unit/security/`, `tests/integration/security/` | every PR |
| Self-performance | PatchFrog analyses a synthetic 5k-file repo within budget; tracks time per phase as a CI artifact (trend, no fixed threshold) | benchmark job | nightly / release |
| Regression | Existing 2,873 tests + oracle eval sanity stay green; the review line is untouched | existing | every PR |

## 2. Rule quality gates (corpus)

- Per rule: precision ≥ 0.90 and recall ≥ 0.80 on the corpus. **Zero findings on clean cases at
  `high` confidence.** A gate failure blocks the merge of a `rule_version` bump.
- Metrics computed with the existing deterministic matcher pattern (`patchfrog/evaluation/matcher.py`
  semantics: file + symbol + rule, no fuzzy matching).
- Corpus results are baselined in `evaluation_baselines/scalability_<version>.json`, with regression
  comparison through the existing `evaluation/regression.py` style thresholds.
- The corpus is authored *before* the rule implementation (test-first) to avoid tuning to the
  implementation.
- Real-world precision (external validation) is reported separately, and never merged with corpus
  numbers.

## 3. Timing-test rules

- No unit test asserts a fixed wall-clock threshold.
- Statistical comparison code is tested on synthetic sample sets (known improvement, known
  regression, overlapping intervals → `inconclusive`, high CV → `unstable_environment`).
- Real timing runs happen only in explicit verification jobs and are marked
  `@pytest.mark.perf`, deselected by default.
- PatchFrog's own local benchmarks are standalone scripts (e.g. `scripts/bench_local_store.py`):
  bounded, host-annotated, never run by pytest or CI. Their results are recorded in the owning
  architecture document (e.g. `LOCAL_STORE.md` §5) as single-host measurements, not claims. Tests
  assert resource *bounds* (e.g. `tracemalloc` peak) and complexity properties (dedup: no
  rewrite), never wall-clock thresholds.

## 4. Reporting test results

Every session report separates (a) **product regressions** (wrong behaviour; tracked in the ROADMAP
stabilization backlog), (b) **environmental failures** (a test needing infrastructure the host
doesn't have; listed in `docs/ci-health.md` → "Known local deviations"), and (c) **documented
skips**. An environmental failure is never counted as passing, and a product regression is never
reclassified as environmental.

Full-suite claims must be backed by a run where every collected node id is accounted for. On
memory-constrained hosts, use `scripts/safe_pytest.py` (`plan` → `run` → `report`). A suite is
"verified" only when `report` shows `not_executed: 0`, `ran_but_not_collected: 0` and
`duplicates: 0`, and every production file changed during the run was covered by a batch that
started after the change (otherwise re-run those files as a delta batch).

## 5. Quality gates per migration phase

Every phase in `docs/ROADMAP.md` must leave all of these green: `ruff check .`,
`mypy . --strict`, `pytest -q` (locally: documented skips and the documented local deviations only;
CI: Postgres and Redis required), the `tests/architecture/` suite (from milestone 1.2), contract
snapshots, plus the phase-specific acceptance tests listed in the roadmap. A change that touches a
Cloud-consumed component also needs the ADR-015 compatibility check. The pinned contracts so far
are `tests/unit/test_core_settings.py` (server env contract) and `tests/unit/test_cli_lazy_worker_imports.py`
(Celery-dispatching CLI paths).
