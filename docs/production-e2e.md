# Production Webhook E2E

How a real GitHub App webhook delivery flows through PatchFrog end to
end, in production-shaped form: signature verification, ingestion,
scheduling, indexing, static analysis, AI review (adaptive context,
Quality + Cost Guard, cooperative specialist orchestration, critic),
persistence, publishing, feedback, and telemetry. This document is the
operational counterpart to `validation/production_e2e/`, which records
one real run of this chain against PatchFrog's own repository.

## The production webhook chain

```
GitHub                                    PatchFrog
------                                    ---------
pull_request event
  --HMAC-signed POST-->  /webhooks/github (apps/api/routes/github_webhooks.py)
                              |  verify_signature() -- reject before parsing
                              |  parse_pull_request_event()
                              v
                          patchfrog.process_pull_request_event (Celery)
                              |  PullRequestIngestionService.ingest()
                              |    - PullRequestIngestionRepository.reserve(delivery_id) -- idempotent
                              |    - fetch PR metadata + changed files (real GitHub API)
                              |    - upsert repositories/pull_requests
                              |  schedule_pipeline_if_eligible() -- patchfrog.ops.eligibility
                              v
                          patchfrog.run_review_pipeline (Celery)
                              |  check_resource_limits() (max changed files / diff bytes)
                              |  _index() -- RepositoryIndexingService
                              |  _analyze() -- StaticAnalysisService (non-fatal on failure)
                              v
                          patchfrog.review_pull_request (Celery)
                              |  PullRequestReviewService.review_pull_request()
                              |    - supersession check (this run's commit vs. current head)
                              |    - ReviewCandidateGenerator (diff-driven, deterministic)
                              |    - ReviewEffortPolicy (Quality + Cost Guard tiering)
                              |    - ContextService (adaptive multi-hop context)
                              |    - AgentOrchestrator (Correctness + Security specialists, critic)
                              |    - validation -> confidence -> dedup -> persistence
                              |  _publication_allowed() gate, then:
                              v
                          patchfrog.publish_review (Celery)
                              |  ReviewPublicationService
                              |    - stale-head check (re-reads live GitHub head)
                              |    - PublicationConfig.enabled gate
                              |    - planner (severity threshold, caps) -> real GitHub write
                              v
                          Real GitHub PR review + inline comments
```

## Required GitHub App events/permissions

Deliberately minimal -- confirmed live via `GET /app` against the real
configured App:

- Permissions: `contents: read`, `metadata: read`, `pull_requests: write`
- Subscribed events: `pull_request` only

No `issues`, `pull_request_review_comment`, or
`pull_request_review_thread` subscription exists -- `patchfrog.feedback`
is deliberately poll-only for reactions/replies/thread state (see
`docs/feedback.md`), never webhook-driven for those signals. Widening
the App's event subscription is out of scope for this milestone and was
not done.

## Worker/services required

- PostgreSQL (single Alembic head, currently `0017_telemetry_intelligence`)
- Redis (Celery broker + result backend)
- API process (`apps.api.main:app`) -- receives and verifies webhooks,
  enqueues Celery tasks, never calls GitHub or an LLM itself
- Worker process (`apps.worker.celery_app`) -- runs the entire chain
  above; the only process that calls the GitHub API or an LLM provider

See `docs/deployment.md` for the full Docker Compose convention. This
milestone's own validation ran the API and worker directly on the host
(via the project's `.venv`) against `docker compose up -d postgres
redis` -- the containerized `api`/`worker` services require a bind-mount
for `GITHUB_PRIVATE_KEY_PATH` (a host path) that isn't configured by
default; using `GITHUB_PRIVATE_KEY` (inline PEM) inside the containers,
or adding that bind-mount, is the containerized equivalent (see the
comment already in `docker-compose.yml`).

## Public webhook endpoint requirements

GitHub must be able to reach `POST /webhooks/github` over HTTPS. In this
environment (no permanently deployed public endpoint), that means a
temporary tunnel (`cloudflared tunnel --url http://localhost:8000`) and
repointing the App's webhook URL via `PATCH /app/hook/config` (App-JWT
auth, `patchfrog.github.auth.build_app_jwt`) -- both actions require
explicit operator approval in this harness; see
`validation/production_e2e/latest-summary.md` for exactly what was
approved and when. A real deployment would instead point the App at a
stable, permanently-reachable ingress.

## Exact-head / supersession semantics

Every stage that matters re-anchors on the exact commit SHA, never "the
PR" as a loose concept:

- Ingestion stores `pull_requests.head_sha` from the webhook payload verbatim.
- `PullRequestReviewService` checks supersession before any provider
  call: if a newer head has already been ingested for this PR, a
  queued-but-stale review is skipped before spending anything on it (see
  `tests/integration/test_review_pull_request_supersession.py`).
- `ReviewPublicationService` re-reads the *live* GitHub head immediately
  before writing (never trusts a possibly-stale DB value) and refuses to
  publish a review whose `commit_sha` no longer matches (see
  `tests/integration/test_publishing_stale_head.py` -- zero GitHub writes
  for a stale head, verified for both `PUBLISH` and `DRY_RUN` modes, and
  for a race arriving after the final pre-write check).

A `synchronize` event is therefore never "the same review continuing" --
it is ingestion of a brand new head, a brand new (or skipped, if
ineligible) pipeline run, and the previous head's completed review
becoming permanently unpublishable once a newer head exists for that PR.

## Publishing safety

Three independent, all-must-pass gates before any real GitHub write:

1. `Settings.global_publication_enabled` -- process-wide kill switch (env var, restart-required).
2. `InstallationModel.publication_allowed` -- per-installation opt-in (DB-persisted, `patchfrog ops installations --allow-publication`, no restart required).
3. `PublicationConfig.enabled` (`.patchfrog.yml`'s `publish: enabled: true`) -- per-repository opt-in, read from the exact commit being published (never the base branch, never cached), defaults to `false`.

A repository's own `.patchfrog.yml` can never force publication when an
operator hasn't independently enabled the first two gates (see the
module docstring of `patchfrog/publishing/config.py`) -- but the reverse
is also true: an operator's `--publish`/`mode=PUBLISH` intent alone is
**not** sufficient either. All three must independently agree. This was
confirmed live during this milestone's own dogfood: the first completed
live review could not be published at all until `.patchfrog.yml` was
added to the reviewed commit, because gate 3 was never configured for
this repository.

## Retry / idempotency

- **Webhook delivery**: `PullRequestIngestionRepository.reserve()` keys
  on `delivery_id`; a redelivered/replayed webhook for an
  already-ingested delivery is recognized as `DUPLICATE` and never
  re-ingests or re-schedules (see `tests/integration/test_ingestion_idempotency.py`).
- **Publish retry**: `ReviewPublicationService` commits a durable
  `PUBLISHING` marker *before* the GitHub write, then reconciles by
  marker on a retry rather than writing again -- a second `publish_review`
  dispatch for the same `(review_run_id, mode, publication_policy_fingerprint)`
  identity never produces a duplicate GitHub review or comment (see
  `tests/integration/test_publishing_concurrency.py`,
  `tests/integration/test_publishing_persistence.py`).
- **Review retry** never re-calls the LLM for an already-`SUCCEEDED`
  canonical run (`review_runs`' canonical identity + `claim_for_write`
  pattern) -- reusing an existing result is always preferred over a
  fresh (paid) call for an identical identity.

## Feedback path

Poll-only, on demand (`patchfrog feedback sync`), never webhook-driven
(see "Required GitHub App events/permissions" above). Every synced raw
signal is either attributed to an exact `ai_findings` row or recorded as
unattributed (`finding_id = NULL`) -- never forced onto a finding it
wasn't confirmed to be about. See `docs/feedback.md` and
`docs/telemetry-intelligence.md`'s "Finding-scoped vs. review-scoped
feedback" section for how both cases are represented downstream.

## Telemetry path

`patchfrog telemetry review <run-id> [--format json]` (or
`patchfrog.telemetry.collector.collect_review_telemetry` directly)
reconstructs a complete, privacy-safe snapshot of any completed review
run purely from already-persisted state -- no re-derivation from raw
webhook payloads, no LLM call, no mutation. See
`docs/telemetry-intelligence.md` for the full model. This document adds
nothing new here; it's the same telemetry layer, now exercised against a
real production-shaped run instead of only fixture/oracle data.

## Live-provider dogfood policy

- Gemini only in this milestone (`PATCHFROG_REVIEW_PROVIDER=gemini`) --
  never Anthropic.
- Operator hard caps (env vars, ceiling a repository's own
  `.patchfrog.yml` can never raise -- see `patchfrog/config/settings.py`
  and `patchfrog.review.config_resolution.apply_operator_hard_caps`):
  `PATCHFROG_MAX_REVIEW_CANDIDATES=2`,
  `PATCHFROG_MAX_TOTAL_INPUT_TOKENS=30000`,
  `PATCHFROG_MAX_OUTPUT_TOKENS_PER_CANDIDATE=2000`,
  `PATCHFROG_MAX_CONCURRENT_REVIEW_REQUESTS=1`,
  `PATCHFROG_MAX_REVIEW_RETRIES=1` (the smallest value `Settings`'
  positivity validator accepts -- `0` is rejected outright, a bounded
  deviation from the literal `0` requested, documented here and in the
  validation artifact).
- Exactly one dogfood PR, a minimum number of live-provider-triggering
  commits, each with a distinct, necessary purpose (never a repeat "for
  nicer numbers") -- see `validation/production_e2e/latest-summary.md`
  for the exact count and why each one was necessary.
- No live benchmark corpus run, no Anthropic call, anywhere in this
  milestone.

## Secret-handling rules

Absolute, for this milestone and going forward:

- Never print/cat `.env`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, the
  GitHub App private key, an installation token, the webhook secret, or
  an `X-Hub-Signature-256` value.
- Never resolve `docker compose config` in a way that echoes a real
  secret value.
- Presence/length-only checks are fine (e.g., confirming a secret field
  is masked or non-empty); values are not.
- These rules apply to this document, the validation artifact, commit
  messages, and PR descriptions equally -- see
  `validation/production_e2e/latest-summary.md`'s own privacy section
  for how the live run was recorded without violating any of them.

## Debugging checklist

1. `GET /health/ready` -- confirms DB migration head + Redis reachability.
2. `celery -A apps.worker.celery_app inspect registered` (subprocess-isolated in tests, live `inspect` locally) -- confirms all 9 tasks are registered.
3. `GET /app/hook/config` (App-JWT) -- confirms the configured webhook URL, without ever printing the masked `secret` field's actual value.
4. API structured logs: `github_delivery_id`, `repository`, `pull_request_number` are bound on every ingestion/scheduling log line -- correlate a real delivery end to end without needing to inspect the raw payload.
5. Worker structured logs: `review_run_id`/`publication_id` appear on every review/publish log line once assigned -- the same correlation continues past ingestion.
6. `patchfrog ops failed [--since ISO]` / `patchfrog ops stale` -- operator recovery commands for a run stuck or failed.
7. `patchfrog telemetry review <run-id>` -- the definitive, privacy-safe record of what a specific run actually did.
