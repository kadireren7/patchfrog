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
  provider actually selected via `PATCHFROG_REVIEW_PROVIDER` is required
  to run the AI reviewer -- default `anthropic`; see
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

**Provider/model selection is an operator deployment concern, not a
repository one.** Which AI provider/model actually runs is chosen via
the `PATCHFROG_REVIEW_*` environment variables below (see
[Provider/model selection](#providermodel-selection-operator-controlled)) --
`.patchfrog.yml` cannot select a provider or a raw model name at all; a
committed `review.provider`/`review.model`/`review.critic_model`/
`review.request_timeout_seconds` field is rejected with a clear,
actionable error (see `patchfrog.review.config.load_review_config`).
`.patchfrog.yml` still controls review *behavior* -- how many candidates
to review, token/concurrency budgets, confidence thresholds, retries.

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
| `ANTHROPIC_API_KEY` | Claude API key. Required when `PATCHFROG_REVIEW_PROVIDER` is `anthropic` (the default). Unset -> `patchfrog.review.provider_factory` raises a clear, actionable error only when a real review is actually requested; nothing silently degrades to a fake provider in production. |
| `GEMINI_API_KEY` | Google Gemini API key. Required only when `PATCHFROG_REVIEW_PROVIDER` is set to `gemini`. Same fail-closed behavior as `ANTHROPIC_API_KEY` -- unset raises `MissingProviderCredentialsError` only when a Gemini review actually runs. |
| `OPENAI_API_KEY` | OpenAI API key (Milestone U). Required only when `PATCHFROG_REVIEW_PROVIDER` is set to `openai`, or when configured as a `PATCHFROG_ROUTER_FALLBACK_PROVIDER`/`PATCHFROG_ROUTER_CRITIC_PROVIDER` (see below). Same fail-closed behavior as the other two. |

PatchFrog's provider architecture is deliberately provider-neutral (see
`patchfrog.review.provider.LLMProvider`) -- deployment configuration
selects the provider/model, never the runtime code. Never paste a
credential into a chat/CLI session to configure this; inject it as a
real secret through your hosting platform's secret manager.

### Provider/model selection (operator-controlled)

Provider/model selection is a trust/cost boundary, not a review-behavior
setting: a reviewed repository must never be able to choose (or
silently influence) which AI provider/model actually runs, since that
would let an untrusted `.patchfrog.yml` route traffic to a different
provider, force a more expensive model, or swap the critic model --
entirely at the operator's expense. It is controlled exclusively by
these environment variables (see
`patchfrog.review.runtime_config.ReviewRuntimeConfig`), resolved
identically by both the CLI and the production Celery worker (one
shared resolver -- they can never diverge):

| Variable | Purpose | Default |
|---|---|---|
| `PATCHFROG_REVIEW_PROVIDER` | `anthropic`, `gemini`, or `openai` | `anthropic` |
| `PATCHFROG_REVIEW_MODEL` | Reviewer model name | `claude-opus-5` |
| `PATCHFROG_REVIEW_CRITIC_MODEL` | Critic model name (optional) | same as `PATCHFROG_REVIEW_MODEL` |
| `PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS` | Per-request timeout, seconds (optional) | `30` (`120` if provider is `gemini`) |

Anthropic (`claude-opus-5`) remains the default -- selecting Gemini is an
explicit operator opt-in:

```
PATCHFROG_REVIEW_PROVIDER=gemini
PATCHFROG_REVIEW_MODEL=gemini-3.6-flash
GEMINI_API_KEY=<secret>
```

That's the whole configuration -- setting only `GEMINI_API_KEY` does
**not** switch the default provider, so existing Anthropic-configured
deployments are never silently affected by adding a Gemini key.
`gemini-2.5-flash` is retired -- Gemini's own API returns `404 NOT_FOUND`
for it as of this writing and recommends `gemini-3.6-flash`, confirmed
live; use the model name above, not the older one.

`resolve_review_runtime_config` fills in provider-coherent effective
values for any of these variables an operator omits, deterministically
(see `patchfrog.review.runtime_config`, and `CONFIG_SCHEMA_VERSION` in
`patchfrog.review.config`, bumped when provider/model selection moved
out of repository-controlled config entirely, so a run canonicalized
under the old repo-controlled semantics is never silently reused):

- **`PATCHFROG_REVIEW_CRITIC_MODEL`**, if unset, defaults to the same
  value as `PATCHFROG_REVIEW_MODEL` -- so Gemini's critic call also asks
  Gemini, never a stale `claude-opus-5` (an earlier version of this
  provider had exactly that bug: an omitted critic model silently kept
  the Anthropic default regardless of provider, so every critic call
  404'd against Gemini's API -- found live, see
  `validation/gemini_provider/quality_sample.json` -- and is now fixed
  at the runtime-config resolution boundary, not documented around).
- **`PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS`**, if unset, defaults to
  120s specifically when `PATCHFROG_REVIEW_PROVIDER=gemini` (30s
  otherwise, unchanged). Gemini 3.6-flash's default thinking behavior is
  slower and far more variable than Anthropic's (single live calls up to
  ~144s were observed, median around 50s) -- 30s produced spurious `504
  DEADLINE_EXCEEDED` failures in testing.

Both remain overridable -- an explicit value always wins over the
provider-appropriate default:

```
PATCHFROG_REVIEW_PROVIDER=gemini
PATCHFROG_REVIEW_MODEL=gemini-3.6-flash
PATCHFROG_REVIEW_CRITIC_MODEL=some-other-valid-gemini-model  # optional override
PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS=60                  # optional override
GEMINI_API_KEY=<secret>
```

A repository's `.patchfrog.yml` cannot set any of these fields --
`review.provider`, `review.model`, `review.critic_model`, or
`review.request_timeout_seconds` in a committed config are rejected
with a clear, actionable error (never silently ignored, never applied)
by `patchfrog.review.config.load_review_config`:

> `review.provider are no longer repository-controlled. Remove these
> fields from '.patchfrog.yml' and configure the PatchFrog
> runtime/operator instead (see docs/deployment.md).`

### Model Router (Milestone U, optional)

Two further environment variables, both optional, both never
`.patchfrog.yml`-controlled, resolved by `patchfrog.routing.router.ModelRouter`
-- see `docs/model-routing.md` for the full architecture:

| Variable | Purpose |
|---|---|
| `PATCHFROG_ROUTER_FALLBACK_PROVIDER` | A single provider to fall back to if `PATCHFROG_REVIEW_PROVIDER` has no credential configured. Bounded to exactly one hop -- no chain to a further fallback. |
| `PATCHFROG_ROUTER_CRITIC_PROVIDER` | Explicitly pin the critic role to a specific provider family (must also have its own credential set). If unset and more than one provider is configured, the router auto-selects a different family than the reviewer for family diversity; with only one provider configured, the critic always uses that same family. |

```
PATCHFROG_REVIEW_PROVIDER=anthropic
ANTHROPIC_API_KEY=<secret>
PATCHFROG_ROUTER_CRITIC_PROVIDER=gemini
GEMINI_API_KEY=<secret>
```

Omitting both leaves routing exactly as it always was before this
milestone: one configured provider serves every role.

**Data policy**: Google's Gemini API free tier states that prompts and
responses may be used to improve Google's products (see Google's current
Gemini API terms). Until an explicit paid-tier/data-processing decision
is made, treat free-tier Gemini as suitable only for public repositories,
PatchFrog's own dogfood, and benchmark fixtures -- **not** for
confidential or private customer code. The free tier's own daily request
quota is also small (20 requests/day per project/model was observed live
for `gemini-3.6-flash`) -- expect it to exhaust quickly even for modest
dogfood use; a paid tier is required for any real usage volume.

### Operator review cost/candidate hard caps (Quality + Cost Guard)

A related but distinct trust boundary from provider/model selection
above: a repository's own `.patchfrog.yml` may still request an
arbitrarily large review-cost/candidate-count budget (`max_candidates`,
`max_total_input_tokens`, `max_output_tokens_per_candidate`,
`max_concurrent_requests`, `max_retries`) unless the operator sets a
hard ceiling. These are environment-only, exactly like provider/model
credentials -- **never** `.patchfrog.yml`-controlled:

| Variable | Purpose | Default |
|---|---|---|
| `PATCHFROG_MAX_REVIEW_CANDIDATES` | Hard ceiling on `ReviewConfig.max_candidates` | `100` |
| `PATCHFROG_MAX_TOTAL_INPUT_TOKENS` | Hard ceiling on `ReviewConfig.max_total_input_tokens` | `1000000` |
| `PATCHFROG_MAX_OUTPUT_TOKENS_PER_CANDIDATE` | Hard ceiling on `ReviewConfig.max_output_tokens_per_candidate` | `16000` |
| `PATCHFROG_MAX_CONCURRENT_REVIEW_REQUESTS` | Hard ceiling on `ReviewConfig.max_concurrent_requests` | `16` |
| `PATCHFROG_MAX_REVIEW_RETRIES` | Hard ceiling on `ReviewConfig.max_retries` | `5` |

`patchfrog.review.config_resolution.apply_operator_hard_caps` computes
`effective = min(repo_intent, operator_hard_cap)` per field, applied by
both the CLI and the production Celery task immediately after
resolving the repository's own config -- a repository may voluntarily
request *less* than these, never more. Defaults are set above
`ReviewConfig`'s own (smaller) defaults, so an unconfigured self-hosted
install behaves exactly as before this feature existed; these only bite
when a repository's own `.patchfrog.yml` asks for something unusually
large. See `docs/quality-cost-guard.md` for the full Quality + Cost
Guard design this trust boundary supports.

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
| `PATCHFROG_VERIFIER_ENABLED` | `false` | Worker only. See [Executable Verification production deployment](#executable-verification-production-deployment) below |
| `VERIFICATION_SNAPSHOT_ROOT` | unset (system temp dir) | Worker only, only relevant when `PATCHFROG_VERIFIER_ENABLED=true`. The worker's own credential-free artifact staging root -- must be the same shared volume/path as the verifier's `VERIFIER_STAGING_ROOT` below |
| `PATCHFROG_VERIFIER_WAIT_TIMEOUT_SECONDS` | `45.0` | Worker only, only relevant when `PATCHFROG_VERIFIER_ENABLED=true` |
| `VERIFIER_STAGING_ROOT` | required (no default) | Verifier only. The one directory the verifier will ever resolve a request's opaque `artifact_id` beneath -- see [Executable Verification production deployment](#executable-verification-production-deployment) |

## Executable Verification production deployment

Milestone S introduced Executable Verification: PatchFrog running one
already-existing, targeted test inside a hardened `bwrap` sandbox to check
a reviewer's own hypothesis. Milestone S6 (Production Execution
Enablement) added a **separate, deliberately credential-minimal verifier
process** (`apps/verifier/`, a second Celery app) so that hostile,
repository-controlled test code never runs in the same process as the
GitHub App private key, provider credentials, or the database
connection -- see `docs/executable-verification.md` and
`validation/production_execution/latest-summary.md` (including its
section 11 security correction round) for the full trust-boundary
rationale.

**The `worker` service never hands the verifier a GitHub credential, or
`.git/` at all.** The worker exports the exact-head commit's tracked
content only (`git archive`, which has no concept of remotes or
credentials) into a fresh, credential-free artifact directory under the
shared volume, and computes a content-manifest digest over it. The
credential-bearing clone the worker used to reach that commit lives in an
isolated, never-shared temp directory and is deleted before the request
is even built. The wire request carries only an opaque `artifact_id`
(a bare directory name, never a filesystem path) plus that digest -- see
"Snapshot integrity, not signing" in `docs/executable-verification.md`.

**Off by default.** `PATCHFROG_VERIFIER_ENABLED=false` (the default) means
the review worker never attempts Executable Verification in production at
all -- not a fallback to less-isolated in-process execution, simply not
attempted. Turning it on requires three things together:

1. `PATCHFROG_VERIFIER_ENABLED=true` and `VERIFICATION_SNAPSHOT_ROOT` set
   on the `worker` service, pointing at a directory shared with the
   `verifier` service (a Docker volume mounted at the identical path in
   both containers -- see the commented-out block in `docker-compose.yml`).
2. `VERIFIER_STAGING_ROOT` set on the `verifier` service to that exact
   same shared path -- required, no default. The verifier resolves every
   request's `artifact_id` strictly beneath this one operator-owned root
   (canonical-path resolution, symlink-escape and `../` traversal both
   rejected as `SANDBOX_ERROR`) -- a request can never select an
   arbitrary host path, and repository-controlled configuration can never
   influence this value.
3. The `verifier` service itself actually running and able to establish
   real sandbox isolation.

**(2) is not automatic under the default containerized deployment.**
Confirmed by direct testing inside a real, non-privileged build of the
`verifier` image: Docker's own default seccomp/AppArmor profile blocks
the mount-namespace operations `bwrap` needs, exactly like the `worker`
image's own embedded sandbox already had this limitation before
Milestone S6 (see `validation/executable_verification/latest-summary.md`
section 22.5 and `validation/production_execution/latest-summary.md`
section 3, Option 3/4). Until an operator makes an explicit
infrastructure decision (see `docs/executable-verification.md`'s
"Production readiness" section), the `verifier` service starts and stays
reachable, but every eligible candidate reports `SANDBOX_ERROR` -- safe,
honest, by-design behavior, not a bug.

The one deployment shape empirically validated end to end by this
milestone is running the verifier as a **bare, non-containerized host
process** (e.g. a systemd service, or a dedicated VM) -- outside any
Docker nesting, `bwrap` is an ordinary unprivileged Linux mechanism with
no special capability requirement at all.

**Staging permissions -- a documented limitation, not a silent gap.**
Architecturally, the worker should write staged artifacts and the
verifier should need only read access to them, with the hostile pytest
subprocess mutating only its own separate disposable copy. On a bare-host
deployment where both processes run as the *same* Unix user (the
simplest, and this milestone's own validated, deployment shape),
filesystem permissions cannot meaningfully separate "worker can write" from
"verifier can only read" -- both processes share one user's write access
to the shared volume. This is compensated for, not ignored: the verifier
always makes its own **disposable copy** of the staged artifact first and
recomputes the content digest over that copy -- never the shared
source -- immediately before executing anything, so a mutation to the
shared artifact after digest computation is either caught by the digest
mismatch or simply never observed by the copy already in flight (never a
silent execution of tampered content). An operator running the worker and
verifier as genuinely separate Unix users (or separate hosts with a
read-only NFS/bind-mount for the verifier's side) gets real OS-level
write separation for free, with no PatchFrog code change.

## Agent Handoff / MCP server (Milestone T)

```
python -m patchfrog.cli mcp serve
```

stdio only -- no port to expose, no separate container or `docker-compose`
service required. Run it on the same host/credentials as the CLI/worker
(it needs the same `Settings`: `DATABASE_URL` plus the GitHub App
credentials, since fix verification re-clones a repository at a new
commit exactly like the review worker does). It reuses the existing S6
verifier boundary for its own Executable Verification re-runs -- if
`PATCHFROG_VERIFIER_ENABLED=true`, fix verification dispatches to the same
separate, credential-minimal verifier process; if not, that one signal is
simply unavailable, exactly like normal review already behaves. See
`docs/agent-handoff.md` for the full trust model, tool surface, and
limitations.

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
  the API itself unable to accept a webhook. Worker availability is
  deliberately never inferred from API readiness either -- the two are
  separate processes/containers; use `patchfrog ops doctor` (below) or
  `celery -A apps.worker.celery_app inspect ping` against the worker
  directly for that.

For a comprehensive, human-readable, secret-safe report covering all of
the above plus GitHub App/provider/model configuration --
`patchfrog ops doctor` (external beta readiness; see
`docs/beta-runbook.md`). It exists specifically because `Settings()`
itself raises a raw `pydantic.ValidationError` for a missing required
variable -- `doctor` catches that and reports each one as an actionable
line instead of crashing before it can check anything else.

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
