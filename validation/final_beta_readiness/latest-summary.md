# Final beta-readiness pass (pre-AA)

Baseline: `main` @ `dcb5fe206e53dc537f9c89b82794907edb364f7d` (Fix critic
false-negative on findings the diff claims are intentional, #62). Branch:
`stabilize/beta-readiness`, already four commits ahead of that baseline
before this pass (`f8fa4cd` CI/repo hygiene, `f2efe48` review cost budgets,
`8a041eb` deterministic reviewer readiness evaluation, `376fd18` idempotent
GitHub review status UX) -- this document covers the audit and the
additional work this pass added on top of those, not a restart.

## 0. Problem restated

Production evidence on `kadireren7/patchfrog` (this repository, self-
reviewing itself): the engine reaches publication but logs
`review_publish_disabled_by_config` -- no `.patchfrog.yml` exists in this
repo at all, and `publish.enabled` defaults to `False`
(`patchfrog/publishing/config.py`). Separately, production is Gemini-only
and the Gemini free tier enforces
`GenerateRequestsPerMinutePerProjectPerModel` (observed ceiling: 5), which
PatchFrog's own specialist-role fan-out (`asyncio.gather` in
`patchfrog/review/orchestration.py`) and retries can exceed on their own
with zero external traffic. Four goals: (A) make GitHub publication
actually turn on for this repo, (B) make the small-PR path safe under a
low Gemini RPM budget, (C) get CI trustworthy (no unexplained red Xs), (D)
prove the GitHub publication lifecycle end-to-end with deterministic,
fake-provider tests -- never a live paid-API call.

## 1. Audit findings (what already existed before this pass)

- **Goal A**: `patchfrog/publishing/config.py` already has the exact
  supported config surface (`publish.enabled`, safe-by-default `False`,
  `.patchfrog.yml`/`.patchfrog.yaml`, `yaml.safe_load` only). No code
  change was needed to make publication *possible* -- only a config file.
  `docs/github-review-ux.md` (added in `376fd18`) and `docs/onboarding.md`
  already document the `checks: write` GitHub App permission requirement
  correctly.
- **Goal B**: `patchfrog/review/effort.py`'s existing LIGHT tier already
  drops the SECURITY role for a small/low-signal candidate, leaving one
  reviewer role by default (pre-existing, Quality + Cost Guard,
  `QUALITY_COST_POLICY_VERSION`). `patchfrog/review/orchestration.py`'s
  `_critique` already returns immediately with zero critic calls when
  there is no valid proposal to critique (`to_critique` empty ->
  `return proposals, 0, 0, ...`) -- "no candidate, no provider call"
  already holds for the critic. `patchfrog/review/provider.py` already
  classified `ProviderInsufficientQuotaError` (fatal, never retried)
  separately from `ProviderRateLimitError` (transient, bounded-retried).
  What did **not** exist: any proactive requests-per-minute ceiling
  (nothing stopped concurrent role fan-out from bursting past a 5 RPM
  quota), and no provider surfaced or honored a `Retry-After`/
  `retryDelay` hint -- every retry used blind exponential backoff.
- **Goal C**: `.github/workflows/ci.yml` (rewritten in `f8fa4cd`) already
  runs a real Postgres service (`PATCHFROG_REQUIRE_POSTGRES=1`, so the DB
  suite never silently skips in CI), verifies `semgrep --version` as a
  required tool, runs `mypy --strict`, checks Alembic has exactly one
  head, and builds both Docker images plus a Celery task-registration
  check. No debug/scratch artifacts were found needing cleanup, and no
  documentation names a required-check string that could drift from
  `ci.yml`'s actual job id (`test`) -- so item 8 (branch-protection/check-
  name doc consistency) had nothing to reconcile. Separately (informational,
  not actioned): `main` currently has **no branch protection rule
  configured at all** (`gh api .../branches/main/protection` -> 404 "Branch
  not protected") -- today a red CI X is advisory only, never blocking. Not
  changed here: enabling branch protection is a repository-administration
  decision with real blast radius (every future PR's merge gate), out of
  this pass's narrow scope.
- **Goal D**: extensive existing coverage: `tests/unit/test_review_checks.py`
  (clean vs. failed vs. partial vs. superseded Check Run states, idempotent
  reconcile on the same head, distinct identity on a new head),
  `tests/unit/test_publishing_planner.py` (inline vs. summary-only
  mapping), `tests/integration/test_publishing_service_publish_e2e.py`
  (real publish writes exactly one review, retry is idempotent),
  `tests/integration/test_publishing_idempotency.py` (GitHub write
  failure -> `ReviewPublicationStatus.FAILED`, retry-safe), and
  `tests/integration/test_ops_eligibility_db.py` (kill switch/suspended
  installation/unselected repository/beta-pending all block before any
  review starts). The one genuine gap: nothing exercised
  `ReviewPublicationService.publish(mode=PUBLISH, config=...)` with
  `publish.enabled=False` and asserted it never writes to GitHub.

## 2. What this pass added

**Goal A** -- `.patchfrog.yml` at the repo root with `publish.enabled:
true`; verified via `load_publication_config` that it resolves correctly.
No other publication control touched (min_severity/caps/frog_marker/
post_clean_summary all untouched defaults) -- narrowest safe scope.

**Goal B** -- new `patchfrog/review/rate_limiter.py`:
`ProviderRateLimiter` (async sliding-window limiter: never more than N
calls in any trailing 60s window; blocks with a single computed
`asyncio.sleep`, never a poll loop), `RateLimitedProvider` (wraps any real
`LLMProvider`, acquiring a slot before every `generate_structured` call --
reviewer/critic/retry/fallback calls all share one wrapped instance),
`ProviderRateLimiterRegistry` (process-wide, one limiter per
`(provider, model)` -- Gemini's own quota unit). Wired into both real
provider-construction points (`patchfrog/routing/router.py`'s
`_build_provider`, the production path, and `patchfrog/review/
provider_factory.py`'s `_build`, the CLI path) via a new operator-only
setting, `PATCHFROG_PROVIDER_RATE_LIMIT_RPM` (JSON dict, keyed like the
existing `PATCHFROG_PROVIDER_PRICING`: `"provider/model"` falling back to
`"provider"`) -- unset means unthrottled, unchanged from before. Scope is
explicit and documented, not hidden: single-process in-memory state, so a
multi-process worker pool gets one independent limiter per process, not
one shared across a deployment; correct for the self-hosted single-worker
case this exists to protect, not claimed to be more.

`patchfrog/review/provider.py`'s `ProviderError` gained an optional
`retry_after_seconds` field; Gemini's adapter now parses
`google.rpc.RetryInfo.retryDelay` out of a 429's error body
(`_parse_retry_delay_seconds`), and Anthropic/OpenAI now read the standard
HTTP `Retry-After` header (`retry_after_seconds_from_http_response`) --
both defensive-by-construction (malformed/missing -> `None`, never a
crash). `patchfrog/review/retry.py`'s `call_with_retry` now honors that
hint over blind exponential backoff when present, capped at
`MAX_RETRY_DELAY_SECONDS = 65.0` so one absurd delay can't single-handedly
consume a run's `max_elapsed_seconds` budget.

**Goal C** -- no code changes were needed (see audit above); this pass's
contribution was running every gate for real, against a real Postgres
service, and reconciling the results:
- `ruff check .` -- clean.
- `semgrep --version` / `ruff --version` -- both present.
- `mypy . --strict` -- clean, 660 source files (663 after this pass's
  additions).
- `alembic upgrade head` against a real `postgres:16-alpine` container --
  applies cleanly; `alembic heads` -- exactly one (`0032_review_cost_budget`).
- `pytest -q` -- 2523 passed, 0 skipped, 0 failed, run with `.env`
  temporarily moved aside (see note below).
- Both Docker images (`api`, `worker`) build clean; Celery task
  registration inside the built worker image reports exactly the expected
  9 tasks.
- `python -m patchfrog.cli eval run` (oracle provider, 5-case sanity
  subset, same as CI) -- precision 1.0 / recall 1.0 / f1 1.0, zero live
  provider calls.

**Local-environment finding, not a CI bug**: running the suite from this
checkout with the developer's own `.env` present (it sets real
`ANTHROPIC_API_KEY`/`GEMINI_API_KEY`) causes 23 spurious failures --
`patchfrog.config.settings.Settings.model_config` sets `env_file=".env"`
unconditionally, so pydantic-settings loads it as a fallback source
*underneath* `os.environ` regardless of what the shell has or hasn't
exported, and several tests (`test_provider_factory.py`,
`test_model_router.py`, `test_ops_doctor.py`) assert "credential absent"
behavior that a real `.env` in the repo root silently defeats. GitHub
Actions CI never has a `.env` file, so it never hits this -- confirmed by
re-running the exact same suite with `.env` moved aside (restored
immediately after each run): 0 failures, 2523 passed. Classified as a
local-development-environment artifact of `env_file=".env"` being a
deliberate, working-as-designed convenience for `docker compose`/local
CLI use, not a product regression -- not changed here.

**Goal D** -- one new test,
`test_publish_mode_with_publication_disabled_by_config_is_explicitly_skipped`
(`tests/integration/test_publishing_service_publish_e2e.py`): calls
`ReviewPublicationService.publish(mode=PUBLISH, config=PublicationConfig())`
(the real production default, `enabled=False`) against a fake GitHub
publisher and asserts `result.status is ReviewPublicationStatus.
SKIPPED_DISABLED`, `github_review_id is None`, and
`publisher.publish_calls == []` -- explicit skip, never a silent success.

## 3. Cloud boundary

`patchfrog-cloud` was not touched. Every change in this pass is either
review-behavior config (`.patchfrog.yml`) or engine code that determines
*how PatchFrog reviews code* (rate limiting, retry classification,
publication gating) -- squarely source-available-engine territory per
`docs/product-boundary.md`, never hosted-SaaS lifecycle/accounting/
GitHub-App-installation-state/deployment config. No reason to cross the
boundary arose.

## 4. Files changed this pass

- `.patchfrog.yml` (new)
- `patchfrog/review/rate_limiter.py` (new)
- `patchfrog/review/provider.py`, `patchfrog/review/retry.py`,
  `patchfrog/review/provider_factory.py`, `patchfrog/routing/router.py`,
  `patchfrog/config/settings.py`
- `patchfrog/review/providers/{gemini,anthropic,openai}_provider.py`
- `.env.example` (documents `PATCHFROG_PROVIDER_RATE_LIMIT_RPM`)
- Tests: `tests/unit/test_provider_rate_limiter.py` (new),
  `tests/unit/test_review_retry_budget.py`,
  `tests/unit/test_review_{gemini,anthropic,openai}_provider_contract.py`,
  `tests/unit/test_model_router.py`,
  `tests/integration/test_publishing_service_publish_e2e.py`
