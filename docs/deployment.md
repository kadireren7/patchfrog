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
- `ANTHROPIC_API_KEY` and/or `GEMINI_API_KEY` (only the key for the
  provider actually selected via `.patchfrog.yml`'s `review.provider` is
  required to run the AI reviewer -- default `provider: anthropic`; see
  [Provider startup/health behavior](#provider-startuphealth-behavior))

Every credential above belongs in your deployment platform's secret/
environment manager -- **never** in Git (`.env` is gitignored;
`.env.example` only ever holds a placeholder), **never** in
`.patchfrog.yml` (a repository-controlled file
`patchfrog.review.config.load_review_config` deliberately never reads
credentials from), and **never** configured per user repository.
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
| `ANTHROPIC_API_KEY` | Claude API key. Required when `.patchfrog.yml`'s `review.provider` is `anthropic` (the default). Unset -> `patchfrog.review.provider_factory` raises a clear, actionable error only when a real review is actually requested; nothing silently degrades to a fake provider in production. |
| `GEMINI_API_KEY` | Google Gemini API key. Required only when `.patchfrog.yml`'s `review.provider` is set to `gemini`. Same fail-closed behavior as `ANTHROPIC_API_KEY` -- unset raises `MissingProviderCredentialsError` only when a Gemini review actually runs. |

PatchFrog's provider architecture is deliberately provider-neutral (see
`patchfrog.review.provider.LLMProvider`) -- deployment configuration
selects the provider/model, never the runtime code. Never paste a
credential into a chat/CLI session to configure this; inject it as a
real secret through your hosting platform's secret manager.

### Selecting Gemini

Anthropic (`claude-opus-5`) remains the production default -- selecting
Gemini is an explicit, per-repository opt-in via `.patchfrog.yml`:

```yaml
review:
  provider: gemini
  model: gemini-3.6-flash
  critic_model: gemini-3.6-flash
  request_timeout_seconds: 120
```

(`gemini-2.5-flash` is retired -- Gemini's own API returns `404 NOT_FOUND`
for it as of this writing and recommends `gemini-3.6-flash`, confirmed
live; use the model name above, not the older one.) Setting only
`GEMINI_API_KEY` in the environment does **not** switch the default --
`.patchfrog.yml` must explicitly request `provider: gemini`, so existing
Anthropic-configured deployments are never silently affected by adding a
Gemini key to the environment.

`critic_model` must be set explicitly too: it defaults to `claude-opus-5`
(the Anthropic default) regardless of `provider`, so a `.patchfrog.yml`
that only sets `provider`/`model` silently sends the critic call to
Gemini's API asking for a nonexistent `claude-opus-5` model. This fails
cleanly (a 404, correctly classified as fatal, gracefully degraded to
no-critic aggregation by `patchfrog.review.service` -- never a crash or a
silent wrong answer) but means critic review never actually runs. This
gap was found live during this provider's own validation -- see
`validation/gemini_provider/quality_sample.json`.

`request_timeout_seconds` also needs raising from the 30s default:
Gemini 3.6-flash's default thinking behavior is slower and far more
variable than Anthropic's (single calls up to ~144s were observed live,
median around 50s) -- 30s produced spurious `504 DEADLINE_EXCEEDED`
transient failures in testing.

**Data policy**: Google's Gemini API free tier states that prompts and
responses may be used to improve Google's products (see Google's current
Gemini API terms). Until an explicit paid-tier/data-processing decision
is made, treat free-tier Gemini as suitable only for public repositories,
PatchFrog's own dogfood, and benchmark fixtures -- **not** for
confidential or private customer code. The free tier's own daily request
quota is also small (20 requests/day per project/model was observed live
for `gemini-3.6-flash`) -- expect it to exhaust quickly even for modest
dogfood use; a paid tier is required for any real usage volume.

### Provider startup/health behavior

A missing `ANTHROPIC_API_KEY`/`GEMINI_API_KEY` deliberately does **not** fail `/health/ready`
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

Before real credentials were ever available in this project's development
environment, live-provider behavior was validated only up to the
credential boundary (`patchfrog.review.provider_factory` raises a clear
`MissingProviderCredentialsError`, never silently falls back to a fake
provider) -- `.env`/`.env.example`/`docker-compose.yml` wiring, secret
redaction, `/health/ready` behavior, and the webhook-to-scheduling path
were all audited end-to-end regardless (`chore/live-runtime-enablement`).

Both providers have since been live-validated with real credentials
(`chore/live-anthropic-validation`, `feat/gemini-provider`): real auth,
real structured-output/schema validation, real token usage, and real
findings against both a direct provider smoke test and a small quality
sample -- see `validation/live_provider/` (Anthropic) and
`validation/gemini_provider/` (Gemini) for full results, including
limitations found and left open (see each summary's own "Remaining
limitations" section).
