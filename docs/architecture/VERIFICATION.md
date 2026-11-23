# Performance Verification Architecture

Goal: replace suspicion with measurement, reproducibly, without PatchFrog becoming a load-testing
product. PatchFrog **orchestrates existing tools**: pytest, SQLAlchemy event hooks, the ASGI test
client (httpx `ASGITransport`), optionally k6, and optionally pyinstrument/py-spy if the user has
them installed. It never builds its own load generator.

## Workflow

```
BASELINE ─▶ ANALYZE ─▶ PROPOSE ─▶ APPLY (agent; user approval) ─▶ VERIFY ─▶ COMPARE ─▶ REPORT
   │           │          │                                         │          │          │
 measure at  static     remediation                               same plan  stats +    artifact +
 revision A  findings   + verification plan                       revision B verdict    markdown
```

1. **Plan** (`PlanVerification`): for each finding, choose a *probe* (query-count, loop-lag,
   http-latency) and generate a **verification plan**: target endpoint(s), seed sizes, the
   command (pytest node or generated harness), environment requirements, budgets. The plan is
   stored and hashed (`plan_hash`). Nothing runs at this stage.
2. **Authorize** (ADR-008): user-level execution enablement plus echoing the `plan_hash` back.
   Execution is refused if any target URL isn't loopback.
3. **Baseline** at revision A (commit SHA + dirty hash recorded). **Verify** at revision B with
   the *identical* plan. Comparison is refused if `plan_hash`, `rule_version`, environment
   fingerprint or seed spec differ (`non_comparable` with reasons).
4. **Compare** produces a verdict per finding: `reproduced`, `verified_fixed`, `not_reproduced`,
   `regressed`, `inconclusive`.

## Probes

| Probe | Signal | Determinism | Used for |
|---|---|---|---|
| `query_count` | SQL statements per request via `event.listen(engine.sync_engine, "before_cursor_execute")`, installed by a PatchFrog pytest plugin fixture | deterministic | PF-SCALE-002, PF-SCALE-003 (row and statement scaling) |
| `rows_and_bytes` | Response row count and body size vs. seed size N | deterministic | PF-SCALE-003 |
| `loop_lag` | Max event-loop lag sampled by a 5 ms ticker task during the request (plus `asyncio` debug slow-callback events) | mostly deterministic (threshold ≥ 50 ms vs. an injected blocking call) | PF-SCALE-001 |
| `latency` | Wall time per request through the ASGI client, in-process | noisy | secondary for all rules |
| `http_load` (optional) | k6 script against a **loopback** server started by the plan | noisy | throughput and concurrency, only when the user asks |

Scaling checks fit statements (or time) against N ∈ {1, 10, 100} (or {100, 1k, 10k} for rows)
and report the slope plus R². "N+1 reproduced" requires slope ≥ 0.9 statements per item.

## Statistics and comparability

- Warm-up: discard the first k = 2 iterations per configuration (import, JIT-free but
  cache-sensitive paths).
- Repeats: N ≥ 5 (configurable). Report median, IQR, min/max, and a bootstrap 95% CI of the
  median ratio B/A.
- Verdict for timing: `improved` only if the whole CI of the ratio is < 1 − ε (default ε = 0.05).
  `regressed` if > 1 + ε. Otherwise `inconclusive`. Deterministic probes override timing when they
  disagree, and the disagreement is recorded.
- Environment fingerprint: Python version, platform, CPU model and count, PatchFrog version, DB
  server version, package lock digest. If it differs, the result is `non_comparable`.
- Variance guard: a coefficient of variation > 0.3 in the baseline marks the timing
  `unstable_environment` (inconclusive).

## Isolation, data and database state

- **Local mode trust model:** the code belongs to the user, who runs it with their approval,
  equivalent to running `pytest` themselves. PatchFrog still applies the env allowlist (no
  PatchFrog secrets), a timeout, output caps, process-group kill and a dedicated temp dir.
- **Database:** verification needs a *disposable* DB. Supported options, in order of preference:
  (1) a test-DB URL the user supplies, used for the project's own pytest fixtures; (2) a
  PatchFrog-started ephemeral Postgres via the `docker` CLI (only with user approval, bound to
  127.0.0.1, removed afterwards); (3) SQLite **only** for `query_count` probes, with the
  dialect-difference caveat recorded. The DB is reset between A and B (fresh schema plus
  deterministic seed with a fixed RNG seed).
- **Why not the existing bwrap sandbox for this:** it uses `--unshare-all` (no network), so it
  can't reach a DB server. It remains the right tool for *hosted/untrusted* execution (Phase 8,
  with a sandbox-local DB). The sandbox core gets extracted in Phase 5 so both paths share
  budgets and process handling.
- **Never:** non-loopback targets, production URLs, automatic execution after analysis, or
  reading `.env` secrets into the subprocess.

## Outputs

`verification/<run_id>/{plan.json, baseline.json, candidate.json, comparison.json, report.md}`.
Each measurement embeds the raw samples, so results can be re-analysed without re-running.
Reports separate **static prediction** (findings) from **measured effect** (comparison), and quote
assumptions verbatim. They never extrapolate to "users supported".
