# PatchFrog

PatchFrog is an open-source, GitHub-native code review engine designed to
combine repository context, static analysis, and AI-assisted reasoning. The
long-term goal is a reviewer that understands your codebase, not just the
diff in front of it, and posts high-confidence inline review comments.

**PatchFrog is early-stage — there is no AI reviewing yet.** This repository
currently implements Phase 1 only: the GitHub ingestion foundation. See
[Non-Goals](#non-goals-for-this-phase) below for exactly what's excluded.

Repository: https://github.com/kadireren7/patchfrog

> This PR exists to live-validate the Phase 1 ingestion pipeline end-to-end
> against a real GitHub App installation. This second commit exercises the
> `synchronize` webhook event.

## Current Phase: Phase 1 — GitHub Ingestion Foundation ✅

Phase 1 establishes the production-minded foundation later phases build on:

- a GitHub App integration (webhook ingestion + installation auth)
- verified, idempotent webhook processing
- a typed GitHub API client
- internal domain models decoupled from GitHub's JSON schema
- a unified-diff parser with a normalized diff representation
- PostgreSQL persistence with Alembic migrations
- a Celery worker pipeline
- a Docker Compose local development environment

At the end of this pipeline, PatchFrog logs a structured summary of an
ingested pull request. **It does not post anything back to GitHub yet.**

## Architecture Overview

```text
apps/
  api/            FastAPI app — HTTP boundary only (webhook receipt, health)
  worker/         Celery app + tasks — thin adapters over services

patchfrog/
  config/         Typed settings (env-driven) + structured logging setup
  domain/         Framework-free internal models (PRs, diffs, GitHub events)
  github/         GitHub boundary: App auth, signatures, REST client, webhook parsing
  diff/           Unified-diff parser + normalized diff models
  persistence/    SQLAlchemy models, async engine/session, repositories
  services/       Use-case orchestration (PullRequestIngestionService)

migrations/       Alembic migrations
tests/            unit/, integration/, fixtures/
```

**Dependency direction:** HTTP/Celery call into `services`, which orchestrate
`domain` models and infrastructure adapters (`github`, `persistence`).
`patchfrog/domain` never imports FastAPI, Celery, or SQLAlchemy — it is pure
Python.

### Webhook lifecycle

```text
GitHub → POST /webhooks/github
       → verify X-Hub-Signature-256 (constant-time HMAC-SHA256)
       → parse event (pull_request: opened/reopened/synchronize; else ignored)
       → enqueue Celery task, respond 202/200/400/401
       → worker authenticates as the GitHub App installation
       → fetches PR metadata + changed files
       → parses unified diffs into normalized DiffFile/DiffHunk/DiffLine
       → upserts Repository/PullRequest, records PullRequestIngestion
       → logs a structured "pull_request_ingested" summary
```

Webhook deliveries are idempotent: `PullRequestIngestion.delivery_id` has a
unique constraint, so a retried GitHub delivery is processed at most once.

## Local Setup

### Prerequisites

- Python 3.12+
- Docker + Docker Compose (for PostgreSQL/Redis, or the full stack)

### Option A — full Docker stack

```bash
cp .env.example .env
docker compose up --build
```

This starts PostgreSQL, Redis, the API (`:8000`), and the worker.

### Option B — local Python, dockerized PostgreSQL/Redis

```bash
cp .env.example .env
make install
docker compose up postgres redis
make migrate
make run       # in one terminal
make worker    # in another
```

### Environment variables

See [`.env.example`](.env.example) for the full list with descriptions:

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` / `test` / `production` |
| `LOG_LEVEL` | Python logging level |
| `DATABASE_URL` | PostgreSQL connection string (`postgresql+asyncpg://...`) |
| `REDIS_URL` | Redis connection string (Celery broker/backend) |
| `GITHUB_APP_ID` | GitHub App ID |
| `GITHUB_PRIVATE_KEY_PATH` | Path to the GitHub App's `.pem` private key file (preferred) |
| `GITHUB_PRIVATE_KEY` | Inline PEM-encoded private key (alternative to the path above) |
| `GITHUB_WEBHOOK_SECRET` | Shared secret used to verify webhook signatures |

Configuration is validated eagerly at process startup — PatchFrog refuses to
start with missing or malformed required settings.

**Private key handling:** set exactly one of `GITHUB_PRIVATE_KEY_PATH` or
`GITHUB_PRIVATE_KEY`. The path form is preferred — it keeps the multiline PEM
out of `.env` and the process environment. Never commit the `.pem` file or
`.env` itself; both are covered by `.gitignore`. When running the Docker
stack, bind-mount the key file read-only rather than baking it into the
image — see the commented example in `docker-compose.yml`.

## Running Tests

```bash
make install
make test
```

Unit tests never touch a real database, Redis, or the GitHub API — GitHub
HTTP calls are mocked with `respx`, and persistence integration tests run
against an in-memory SQLite database. The test-only RSA key used to sign
JWTs in tests is generated in memory at test-session start, never committed.
Run `make lint` (ruff) and `make typecheck` (mypy --strict) alongside
`make test`.

## GitHub App Setup

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Set **Webhook URL** to `https://<your-tunnel-or-host>/webhooks/github` and
   generate a **Webhook secret** — put it in `GITHUB_WEBHOOK_SECRET`.
3. Grant **Repository permissions → Pull requests: Read-only** and
   **Contents: Read-only**.
4. Subscribe to the **Pull request** event.
5. Generate a **private key** (downloads a `.pem` file), and set
   `GITHUB_APP_ID` plus either `GITHUB_PRIVATE_KEY_PATH` (pointing at the
   downloaded file) or `GITHUB_PRIVATE_KEY` (its contents inline).
6. Install the App on a target repository and open a pull request —
   PatchFrog should log `pull_request_ingested` once the worker processes
   the event.

### Exposing your local webhook endpoint

GitHub needs an HTTPS URL it can reach, so a local `localhost:8000` needs a
tunnel during development — e.g. [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
(`cloudflared tunnel --url http://localhost:8000`) or [ngrok](https://ngrok.com/).
Use the tunnel's public HTTPS URL (with `/webhooks/github` appended) as the
GitHub App's Webhook URL.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Roadmap

- [x] **Phase 1** — GitHub ingestion foundation
- [ ] **Phase 2** — Repository indexing
- [ ] **Phase 3** — Static analysis
- [ ] **Phase 4** — Context engine
- [ ] **Phase 5** — AI reviewer
- [ ] **Phase 6** — Verification / confidence scoring
- [ ] **Phase 7** — Incremental review memory
- [ ] **Phase 8** — Autofix / test generation

### Non-goals for this phase

No LLM/AI provider integration, embeddings, vector search, static analysis
tooling (Tree-sitter/Semgrep/clang-tidy), inline review comments, autofixes,
test generation, dashboards, or multi-repository analysis exist yet. These
belong to later phases.
