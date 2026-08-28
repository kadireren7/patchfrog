# Deployment

PatchFrog is four components, all cloud-neutral -- nothing here is
hard-wired to a specific hosting provider.

| Component | What it is | Image target |
|---|---|---|
| API | FastAPI app, receives GitHub webhooks | `docker/Dockerfile` target `api` |
| Worker | Celery worker, runs the actual pipeline | `docker/Dockerfile` target `worker` |
| PostgreSQL | Primary datastore (16+) | — |
| Redis | Celery broker + result backend | — |

An optional reverse proxy / managed ingress terminates TLS and forwards
`/webhooks/github` to the API service; PatchFrog itself never handles
TLS termination.

## Local production-like stack

```
docker compose up -d postgres redis
alembic upgrade head            # see "Migration process" below
docker compose up -d api worker
```

`docker-compose.yml` at the repo root runs all four components with
production-like configuration (real Postgres, real Redis, no mocks for
core infrastructure). The LLM provider is the only thing ever faked in
this stack, and only when `ANTHROPIC_API_KEY` is unset -- see
[Live model support](#live-model-support) below.

## Required runtime secrets

Concise summary (see the full tables below for every other variable):

- `GITHUB_APP_ID`
- `GITHUB_PRIVATE_KEY_PATH` or `GITHUB_PRIVATE_KEY` (exactly one)
- `GITHUB_WEBHOOK_SECRET`
- `ANTHROPIC_API_KEY` (required only to actually run the AI reviewer --
  see [Provider startup/health behavior](#provider-startuphealth-behavior))

`ANTHROPIC_API_KEY` belongs in your deployment platform's secret/
environment manager, exactly like the three GitHub credentials above --
**never** in Git (`.env` is gitignored; `.env.example` only ever holds a
placeholder), **never** in `.patchfrog.yml` (a repository-controlled
file `patchfrog.review.config.load_review_config` deliberately never
reads credentials from), and **never** configured per user repository.
PatchFrog's hosted service always uses the operator's own provider
credential for every installation -- there is no per-repository
bring-your-own-key model, and none is planned.

## Required environment variables

Startup fails clearly (a `pydantic.ValidationError` at process start,
never a silent fallback to unsafe demo settings) if any of these are
missing:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `REDIS_URL` | `redis://...` |
| `GITHUB_APP_ID` | GitHub App ID |
| `GITHUB_PRIVATE_KEY` or `GITHUB_PRIVATE_KEY_PATH` | Exactly one -- inline PEM or a path to one |
| `GITHUB_WEBHOOK_SECRET` | Verifies webhook signatures |

## Provider-specific (optional, but required to actually review)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key. Unset -> `patchfrog.review.provider_factory` raises a clear, actionable error only when a real review is actually requested; nothing silently degrades to a fake provider in production. |

PatchFrog's provider architecture is deliberately provider-neutral (see
`patchfrog.review.provider.LLMProvider`) -- deployment configuration
selects the provider/model, never the runtime code. Never paste a
credential into a chat/CLI session to configure this; inject it as a
real secret through your hosting platform's secret manager.

### Provider startup/health behavior

A missing `ANTHROPIC_API_KEY` deliberately does **not** fail `/health/ready`
or block the process from starting -- confirmed and preserved as-is
(spec section 5). `/health/ready` fails closed only for what makes the
API itself unable to accept a webhook: database reachability, migration
revision, Redis reachability (see [Health endpoints](#health-endpoints)).
Static analysis and webhook ingestion stay fully available with no
provider credential configured at all. Instead, the failure surfaces
exactly where it's actionable: `patchfrog.review.provider_factory`
raises `MissingProviderCredentialsError` only when a real AI review is
actually about to run, which the Celery task classifies as a normal
`FAILED` review run with a clear, non-retryable reason (see
`patchfrog.ops.errors.classify_exception`) -- visible via `patchfrog ops
failed`, never a silent no-op and never a process-wide outage.

## Optional operational settings

All have conservative defaults; see `patchfrog/config/settings.py` for
the full list and current defaults. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `LOG_LEVEL` | `INFO` | |
| `BETA_ALLOWLIST_MODE` | `false` | New installations start `pending` instead of self-serve `active` |
| `GLOBAL_REVIEW_PROCESSING_ENABLED` | `true` | Emergency kill switch, see [Kill switches](operations.md#kill-switches) |
| `GLOBAL_PUBLICATION_ENABLED` | `true` | Same, for the publish stage specifically |
| `MAX_CHANGED_FILES` | `300` | PRs over this are skipped, never partially reviewed |
| `MAX_DIFF_BYTES` | `2_000_000` | Same, for total diff size |
| `DEFAULT_DAILY_REVIEW_LIMIT` | `50` | Per-installation quota unless overridden |
| `STALE_RUN_THRESHOLD_MINUTES` | `60` | See `patchfrog ops stale` |
| `PROMETHEUS_MULTIPROC_DIR` | unset | Worker only. Required for the worker's own `:9100/metrics` to report anything -- see [Metrics](#metrics) |
| `WORKER_METRICS_PORT` | `9100` | Worker only, only relevant when `PROMETHEUS_MULTIPROC_DIR` is set |

## Migration process

**Never let every API/worker instance race to run `alembic upgrade`
concurrently at startup.** Run migrations once, explicitly, as a
separate deploy step before rolling out new API/worker instances:

```
alembic upgrade head
```

Deploy order:

1. `alembic upgrade head` (one-shot, from a deploy pipeline or a single
   admin shell -- never baked into the API/worker container's own
   startup command).
2. Roll out the new worker image.
3. Roll out the new API image.

`GET /health/ready` (see below) fails closed if the database's applied
migration revision doesn't match what the running code expects --
catching a skipped or out-of-order migration step immediately, rather
than surfacing as a confusing runtime error later.

## Health endpoints

- `GET /health/live` -- process alive. Never touches the database,
  Redis, GitHub, or the LLM provider. Use for a container orchestrator's
  restart decision.
- `GET /health/ready` -- database reachable *and* on the expected
  migration revision, Redis reachable. Returns HTTP 503 (never 200) on
  any failure. Use for a load balancer's routing decision. Deliberately
  never checks GitHub or the LLM provider -- their outages don't make
  the API itself unable to accept a webhook.

## Metrics

**Two separate scrape targets, not one** -- found by dogfooding the
local stack: every review/pipeline/publication metric is incremented
from worker-side task code, and `prometheus_client`'s registry lives in
one process's memory, never shared across processes (let alone separate
containers). Scrape both:

- `GET http://<api>:8000/metrics` -- the API process's own endpoint.
  Legitimately empty for every review/pipeline/publication counter --
  the API process never increments any of them, it only serves this
  route. Still worth scraping for the process-level Python/GC metrics
  `prometheus_client` adds automatically.
- `GET http://<worker>:9100/metrics` -- the worker container's own
  aggregating endpoint, live from the moment a task actually runs. Only
  bound when `PROMETHEUS_MULTIPROC_DIR` is set (the worker service in
  `docker-compose.yml` sets it); requires no code change to any
  individual counter/histogram definition, see `patchfrog/ops/metrics.py`.

Every metric on both endpoints is low-cardinality and contains no
repository names, usernames, source code, or finding text. In
production, restrict both endpoints at the network/ingress level to
your metrics scraper -- neither is authenticated at the application
layer.

## Docker image notes

- Both `api` and `worker` targets run as an unprivileged user
  (`USER patchfrog`), never root.
- No build secrets are baked into any layer -- credentials are always
  injected as runtime environment variables.
- The `worker` image bundles `git`, `cppcheck`, and `clang-tidy` (static
  analyzers PatchFrog shells out to); `ruff` and `semgrep` are installed
  as Python dependencies in both images.
- Both images declare a `HEALTHCHECK` (`GET /health/live` for the API,
  `celery inspect ping` for the worker).
- Graceful shutdown: Celery's default `SIGTERM` behavior (warm
  shutdown -- stop accepting new tasks, let in-flight ones finish) is
  used unchanged; no custom signal handling was added. A worker killed
  mid-task (`SIGKILL`, OOM) leaves that one review run `RUNNING`
  indefinitely -- see `patchfrog ops stale` in `docs/operations.md` for
  recovery.

## Live model support

`ANTHROPIC_API_KEY` has never been available in this project's own
development environment, so live-provider behavior is validated only up
to the credential boundary (`patchfrog.review.provider_factory` raises a
clear `MissingProviderCredentialsError`, never silently falls back to a
fake provider). This is an explicit, documented stopping point --
productionization was never blocked on it, and a hosted deployment
simply needs to inject a real key through its own secret manager.

Everything up to that boundary has been audited end-to-end (chore/
live-runtime-enablement): `.env`/`.env.example`/`docker-compose.yml`
wiring, secret redaction (structured logs, `Settings.__repr__`, provider
exceptions), `/health/ready` behavior, and the webhook-to-scheduling path
for arbitrary branches -- see [Required runtime
secrets](#required-runtime-secrets), [Provider startup/health
behavior](#provider-startuphealth-behavior), and `docs/onboarding.md`'s
"Branch scope" section. The only remaining gap to actually prove a live
review end-to-end is a real key in this environment; once one is
injected, `patchfrog.cli review --provider anthropic` (or a real webhook
delivery) exercises the exact same code path validated here with
`FakeLLMProvider`.
