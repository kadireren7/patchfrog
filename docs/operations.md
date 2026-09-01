# Operations

## Pipeline overview

```
GitHub webhook (pull_request)
  -> apps.api.routes.github_webhooks (signature verify, fast 202 ack)
  -> patchfrog.process_pull_request_event (ingest PR metadata)
  -> patchfrog.ops.eligibility.check_eligibility (installation/repo/quota/kill-switch gate)
  -> patchfrog.run_review_pipeline (resource-limit check, then index -> analyze)
  -> patchfrog.review_pull_request (supersession re-check, then the AI review)
  -> patchfrog.publish_review (beta + repo publication gates, then the real GitHub write)
```

Every arrow after ingestion is an automatic Celery task hand-off (see
`patchfrog/ops/orchestrator.py`) -- installing the App and opening a PR
is enough; nothing here requires a human to manually trigger a stage.

Feedback sync (`patchfrog ops`... actually `patchfrog feedback sync`,
Phase 9) remains a separate, on-demand poll -- see `docs/feedback.md`.

## Error taxonomy

Every pipeline-stage failure is classified into one of (see
`patchfrog/ops/errors.py`):

```
GITHUB_AUTH_ERROR        GITHUB_RATE_LIMIT       REPOSITORY_FETCH_ERROR
INDEXING_ERROR           STATIC_ANALYSIS_ERROR   PROVIDER_ERROR
PROVIDER_RATE_LIMIT      PROVIDER_TIMEOUT        VALIDATION_ERROR
PUBLICATION_ERROR        DATABASE_ERROR          INTERNAL_ERROR
```

Never collapsed to "review failed" -- every failure log line and the
`patchfrog_reviews_failed_total{error_category=...}` metric carry the
category. Retryability is decided per-exception (a `GitHubNotFoundError`
and a `GitHubServerError` are both `REPOSITORY_FETCH_ERROR`, but only the
latter is retried) -- see `classify_exception`'s docstring.

## Retry policy

Bounded exponential backoff with jitter, never infinite:

| Task | Retries | Backoff |
|---|---|---|
| `run_review_pipeline` | 3 | 15s, capped at 300s |
| `publish_review` | 5 | 30s, capped at 600s |

Non-retryable failures (malformed config, invalid credentials, a 404/422
from GitHub) fail once and stay failed -- see `patchfrog ops failed`.

## Resource limits and quotas

Checked once, before indexing starts (`patchfrog.ops.eligibility`):

- `MAX_CHANGED_FILES` / `MAX_DIFF_BYTES` -- an oversized PR is skipped
  entirely (`pull_request_resource_limit_exceeded` log line), never
  partially reviewed.
- Per-installation daily review quota (`DEFAULT_DAILY_REVIEW_LIMIT`, or
  `InstallationModel.daily_review_limit` to override one installation) --
  counted from real `review_runs` rows in the last 24h, no separate
  counter table to keep in sync.

## Fairness

`PER_INSTALLATION_CONCURRENT_REVIEW_LIMIT` documents the intended
per-installation concurrency cap; global fairness in this beta comes
primarily from the daily quota above plus ordinary Celery worker
concurrency -- a dedicated per-installation concurrency *enforcement*
queue was judged unnecessary complexity for a beta-scale deployment and
is not implemented; see "Remaining limitations" in the PR description.

## Supersession

A commit superseded by a newer one before the AI review stage starts is
detected by re-fetching the PR's live head SHA and comparing it to the
queued commit -- if they differ, the review is skipped
(`review_skipped_superseded`, `patchfrog_reviews_skipped_total{reason="superseded"}`)
before a single provider call is made. No database row is created for a
skipped commit -- Phase 5's run-identity/locking machinery is untouched.
Phase 6's own stale-head protection (a `STALE` publication status,
zero GitHub writes) independently guards the publish stage against the
same race.

## Kill switches

| Scope | Mechanism | Requires restart? |
|---|---|---|
| Global (review processing) | `GLOBAL_REVIEW_PROCESSING_ENABLED` env var | Yes |
| Global (publication) | `GLOBAL_PUBLICATION_ENABLED` env var | Yes |
| Per-installation | `patchfrog ops installations --suspend <id>` | No |
| Per-repository | GitHub's own "deselect repository" (or an operator flipping `is_selected` directly) | No |

The global switches are intentionally the coarse, rarely-touched
emergency stop; per-installation/per-repository control is the
day-to-day lever and never requires a deploy.

## Stale-run recovery

A review run still `RUNNING` past `STALE_RUN_THRESHOLD_MINUTES` (default
60) is almost always a crashed worker or a lost task:

```
patchfrog ops stale              # list
patchfrog ops stale --recover    # mark each one FAILED
```

`--recover` only ever changes local run state to `FAILED` -- it never
publishes, retries, or touches GitHub. Re-run the pipeline for a
specific commit afterward with `patchfrog ops retry <review_run_id>`.

## Operations CLI

```
patchfrog ops health                 # DB/Redis/migration readiness
patchfrog ops stale [--recover]      # runs stuck RUNNING past the threshold
patchfrog ops failed [--since ISO]   # failed review runs, with error detail
patchfrog ops retry <review_run_id>  # re-enqueue the pipeline for that commit
patchfrog ops usage                  # per-installation quota usage (24h)
patchfrog ops installations          # list, or --activate/--suspend/--allow-publication
patchfrog telemetry review <run-id> [--format text|json] [--output PATH]
                                      # deterministic telemetry snapshot for one review run
                                      # -- see docs/telemetry-intelligence.md
```

No command mutates GitHub directly, and no command is destructive to
already-published GitHub content -- `ops retry` only re-enqueues local
work.

## Metrics

Prometheus text format (`patchfrog/ops/metrics.py`):

```
patchfrog_reviews_started_total / _completed_total{status} / _failed_total{error_category} / _skipped_total{reason}
patchfrog_review_duration_seconds
patchfrog_repository_index_duration_seconds
patchfrog_static_analysis_duration_seconds
patchfrog_publication_duration_seconds
patchfrog_provider_calls_total{provider,model,role} / _input_tokens_total / _output_tokens_total
patchfrog_findings_generated_total / _published_total / _suppressed_total{reason}
patchfrog_feedback_events_total{event_type}
patchfrog_candidates_by_tier_total{tier}          # Quality + Cost Guard tier distribution
patchfrog_candidates_skipped_budget_total          # candidates skipped for run-level token budget
patchfrog_critic_calls_total                       # critic verification calls made
```

The three tier/budget/critic counters above (Evaluation & Telemetry
Intelligence milestone) are deliberately the only *aggregate* operational
signal this milestone adds to Prometheus -- `tier` is a closed 3-value
set (`light`/`standard`/`deep`), and none of the three carry a repository
name, PR number, candidate id, finding id, or file path. Per-run,
per-candidate, and per-finding detail (which tier a specific candidate
landed on, which role produced which finding, per-role token/latency
breakdowns) belongs in the telemetry snapshot
(`patchfrog telemetry review <run-id>`, see
[docs/telemetry-intelligence.md](telemetry-intelligence.md)), never in a
Prometheus label -- that is exactly the line this milestone draws
between "low-cardinality live-ops signal" and "detailed analysis."

**Read this from `:9100/metrics` on the worker, not `:8000/metrics` on
the API** -- every counter above is incremented from worker-side task
code, and lives in that process's memory. The API's own `GET /metrics`
serves the same registry shape but stays at zero for all of these
forever; it's a separate process that never increments them. Found by
dogfooding the local stack: the API's `/metrics` still read `0` moments
after a real review had already started in the worker container. Fixed
via `prometheus_client`'s documented multiprocess mode
(`PROMETHEUS_MULTIPROC_DIR`, set for the worker service in
`docker-compose.yml`) plus a small aggregating HTTP server
(`patchfrog.ops.metrics.start_worker_metrics_server`, wired to Celery's
`worker_init` signal in `apps/worker/celery_app.py`) that reads across
every one of the worker container's forked prefork subprocesses. See
[Metrics](deployment.md#metrics) in `docs/deployment.md` for the
two-target scrape configuration.

Every label is a bounded, closed set (status/category/provider/model
name) -- never a repository name, installation id, username, or any
finding/source text. `context_duration_seconds` and
`ai_review_duration_seconds` are defined in the registry for future use
but not yet independently instrumented (context building and the AI
call happen inside Phase 4/5's existing service, not exposed as a
separate timing at the task layer today) -- `review_duration_seconds`
already covers the whole review stage.

## Structured logging

JSON via `structlog` (`patchfrog/config/logging.py`). Every pipeline log
line carries correlation fields where available: `repository`,
`pull_request_number`, `commit_sha`, `review_run_id`, `installation_id`,
`github_delivery_id`. A defense-in-depth redaction processor
(`redact_secrets`) runs on every log call: field names shaped like
`*token*`/`*secret*`/`*key*`/`authorization`/`password` are replaced
outright, and PEM blocks/`Bearer ...` tokens/GitHub token prefixes
(`gh?_...`) are redacted wherever they appear, including inside a
formatted exception traceback -- never relied upon as the *only*
safeguard (no call site in this codebase attaches a raw secret to a log
call in the first place; see `patchfrog.config.settings`), but a
processor that always runs catches a future mistake before it reaches
stdout.

## Known security-boundary limitations

- **No explicit byte-size cap on `.patchfrog.yml`/`.patchfrog.yaml`
  before parsing**: all four loaders (`review`/`analysis`/`publishing`/
  `review_memory` config) read the whole file into memory and call
  `yaml.safe_load` on it, with no size check beforehand. `safe_load`
  itself is not vulnerable to a classic "billion laughs" expansion
  (PyYAML resolves anchors/aliases as shared references, not deep
  copies -- see `tests/integration/test_security_boundaries.py`'s
  `test_yaml_alias_reuse_does_not_multiply_memory`), so the only real
  exposure is an attacker-controlled repository committing a very large
  flat file, bounded only by available worker memory. Judged low-risk
  for beta (the installing party already controls their own repository
  content) and not fixed here -- fixing it well means touching four
  near-duplicate parsing call sites, which is architecture-adjacent
  surface this phase deliberately didn't take on. Revisit if beta usage
  ever includes repositories PatchFrog doesn't trust the owner of.

## Data retention

- **Temporary repository checkouts** (`patchfrog-snapshot-*` under the
  configured work directory): deleted on every exit path -- success,
  failure, or exception -- via `RepositorySnapshot`'s context-manager
  `cleanup()`. A worker killed with `SIGKILL` mid-checkout is the one
  path this can't guarantee; treat container-local disk as ephemeral and
  clean on restart (no periodic sweep job is shipped for this beta).
- **Database rows retained indefinitely** during beta: `ai_findings`,
  `feedback_events` (append-only, tombstone-preserving), review run
  metadata, publication records. No PII beyond a GitHub login and
  whether it's a bot (see `docs/feedback.md`'s privacy section) is ever
  stored.
- **Raw AI prompts/responses**: not persisted; only the structured,
  validated finding fields are (`ai_finding_proposals`/`ai_findings`).

## Troubleshooting

| Symptom | Check |
|---|---|
| PR opened, no review appears | `patchfrog ops failed`; `patchfrog.ops.eligibility` log line (`pull_request_ineligible`) for the reason |
| Review happens, no GitHub comment | Both publication gates -- `patchfrog ops installations` for `publication_allowed`, and the repository's `.patchfrog.yml` `publish.enabled` |
| `GET /health/ready` returns 503 | Response body names which check failed (`database`/`redis`) and, for database, whether it's a migration mismatch |
| A review run stuck `RUNNING` | `patchfrog ops stale` |
