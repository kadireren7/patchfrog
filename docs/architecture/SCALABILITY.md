# PatchFrog's Own Scalability: Execution, Budgets, Concurrency, Storage

This document covers how PatchFrog itself scales. The rules it applies to *other* code are in
`DETECTION_RULES.md`. The principle is to scale through boundaries and contracts first and
infrastructure later, adding infrastructure only when a measured requirement exists.

## 1. Workload characteristics

| Workload | Shape | Bottleneck | Notes |
|---|---|---|---|
| Static analysis | CPU-bound parse and graph build, O(files + calls) | single core per run (GIL) | Deterministic and cacheable by content hash |
| Verification | Wall-clock-bound subprocesses, sensitive to noise | machine resources, measurement interference | Must **not** run concurrently with other measurements on the same host |
| MCP reads | Small JSON reads from artifacts | disk | trivial |

## 2. Job model

- `Job(kind, idempotency_key, budget, payload)` → `JobRunner.submit()` → `RunHandle`.
- Lifecycle: `queued → running → succeeded | partial | failed | cancelled`, plus `interrupted`
  for a dead owner. Every transition is persisted atomically, as artifact `run.json` (local) or a
  row (server). **Implemented (2.2):** `running → succeeded | partial | failed | cancelled`, with no
  `queued` state yet (the runner is synchronous; the queue arrives with MCP, Phase 4).
- **Idempotency:** `idempotency_key = sha256(revision.files_digest, config_fingerprint,
  engine_version, ruleset_version, scope)`. When a succeeded run with the same key exists it is
  returned without recomputing. `partial`/`failed` runs are never reused.
- **Restart safety:** a run left `running` by a dead process is marked `interrupted` by the next
  `JobRunner` (via `LocalRunStore.recover()`; liveness uses `flock`, not PIDs). Jobs are pure, so
  re-running them is always safe.
- **Failure classes** (closed enum): `input_error` (bad path or config), `unsupported_project`,
  `budget_exhausted`, `tool_unavailable` (pytest/docker/k6 missing), `execution_failed` (non-zero
  exit), `environment_unstable` (variance guard), `interrupted`, `internal_error`. A failure is
  never reported as clean.

## 3. Budgets (all configurable, all reported in `budget_usage`)

| Budget | Default (analysis) | Behavior when exceeded |
|---|---|---|
| `max_files` | 5,000 Python files | stop discovery, `partial: max_files` |
| `max_file_bytes` | 1 MiB (as `MAX_GRAPH_FILE_BYTES`) | skip file, recorded in coverage |
| `max_total_bytes` | 200 MiB | `partial` |
| `max_seconds` | 60 s (MCP), 600 s (CLI) | cooperative stop at the next checkpoint |
| `max_trace_depth` | 4 | trace truncated, recorded |
| `max_findings` | 500 | lowest-confidence findings dropped, count reported |
| verification `max_seconds` | 300 s per command | process-group kill, `failed: timeout` |
| subprocess output cap | 5 MiB per stream (existing sandbox) | truncated flag |

## 4. Concurrency and backpressure

- **Local:** one analysis per idempotency key at a time, using an advisory file lock
  (`fcntl.flock`) on one of 256 striped lock files in the store (`LocalRunStore.request_lock`,
  implemented in 2.2). Parsing parallelism through a `ProcessPoolExecutor` with
  `max_workers = min(4, os.cpu_count())`, opt-in after Phase 3 profiling shows a benefit. Never
  more than one verification job per host at a time (a global lock), because concurrent
  measurement corrupts timing.
- **MCP:** a bounded in-process queue (default capacity 4). When it's full, requests get a
  `budget_exhausted` error with `retryable: true`, never unbounded task creation.
- **Server mode (later):** the same job functions run as Celery tasks with `acks_late`, a
  visibility timeout above the job budget, and a per-queue concurrency limit. Postgres idempotency
  uses a unique index on `(idempotency_key) WHERE status='succeeded'` plus the existing
  advisory-lock pattern.

## 5. Storage

- **Local:** immutable artifacts (ADR-002). Content-addressed cache entries for parsed files
  (`content_hash + PARSER_VERSION + semantics_version`) make re-analysis incremental. Retention:
  `patchfrog cache prune --keep-last N --older-than D`.
- **Server:** Postgres (ADR-003). Evidence blobs larger than 256 KiB go to an object store
  interface (`BlobStore` protocol: filesystem implementation first, S3-compatible later), never
  into rows.
- Evidence is immutable and tied to `RepositoryRevision`. Corrections create a new run and never
  mutate an old one.

## 6. Observability

- `structlog` JSON logs with `run_id`, `job_kind`, `phase`, `duration_ms` and `budget_usage`.
  Never source contents or secret values.
- Per-run `metrics` in the artifact: files parsed, cache hit rate, time per phase, findings per
  rule. These come from the Phase 3 profiling baseline and drive optimization of PatchFrog itself.
- Server mode reuses `patchfrog.ops.metrics` (Prometheus). Local mode keeps no metrics endpoint.

## 7. Path to horizontal scale (only when measured)

| Trigger (measured) | Step |
|---|---|
| A single run exceeds the budget on real repos | profile, then incremental cache, then process-pool parsing |
| Many concurrent users in server mode | N stateless Celery workers on Postgres (already supported topology) |
| Queue latency SLO violated with N workers on one host | more hosts. Workers are stateless, and artifacts go to the BlobStore |
| Untrusted multi-tenant execution | hosted sandbox fleet (Cloud repo, not this repo) |

Kubernetes, Kafka and microservices aren't on this path without a documented requirement that
the steps above can't meet.
