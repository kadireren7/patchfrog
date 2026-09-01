# External Beta Readiness — Validation Summary

Branch `chore/external-beta-readiness`, baseline `main` @
`e452940790014aac7b5edab9f1f4b8b5cf7155ad` (Milestone H, merged). No
dogfood PR was needed this milestone — see "Dogfooding the onboarding
flow" below for what was proven instead, live and read-only, against
PatchFrog's own real GitHub App and repository (`kadireren7/patchfrog`).

## 1. Onboarding-surface audit (before any code change)

Read in full before writing anything: README, `docs/onboarding.md`,
`docs/deployment.md`, `docs/operations.md`, `docs/production-e2e.md`,
`docs/licensing.md`, `docs/product-boundary.md`, `docs/feedback.md`,
`docs/telemetry-intelligence.md`, `.env.example`, `docker-compose.yml`,
`patchfrog/config/settings.py`, `patchfrog/ops/{eligibility,health,queries}.py`,
`patchfrog/publishing/{body,config}.py`, `patchfrog/cli.py`,
`patchfrog/ops/errors.py`, `.gitignore`, `pyproject.toml`, plus the
existing eligibility/installation-sync/publishing test suites.

**Headline finding: this project's onboarding/operations documentation
was already unusually thorough** — most of what a typical "external
beta readiness" milestone would need to build from scratch already
existed: three independent, defense-in-depth publication gates (already
implemented and mostly documented), per-installation/per-repository
kill switches, a beta allowlist mode, resource limits and daily quotas,
a structured error taxonomy with retry classification, secret-redacting
structured logging, and an operations CLI covering health/stale/failed/
retry/usage/installations. Per the milestone's own repeated instruction
("if this already exists, do not redesign it"), none of that was
touched or duplicated.

**Real gaps found, all addressed below:**

1. **`patchfrog ops health` crashes ungracefully on a fresh/incomplete
   `.env`.** `Settings()` raises a raw multi-error `pydantic.ValidationError`
   for missing required fields — confirmed by direct reproduction
   (`Settings()` with an empty environment) before writing any fix. A
   first-time external operator's very first diagnostic command would
   have died with an unfriendly traceback before checking anything.
2. **`docs/onboarding.md` said "two independent gates" when the code
   (and `docs/production-e2e.md`, correctly) has three** — the global
   `GLOBAL_PUBLICATION_ENABLED` kill switch was omitted from
   onboarding.md's own enumeration, exactly the kind of "ambiguous
   docs" this milestone's audit exists to catch. This was also the
   *exact* class of confusion Milestone H's own dogfood ran into live.
3. **No single command answered "will a PR against this specific
   repository actually publish right now"** — an operator had to
   cross-reference `patchfrog ops installations` (DB state) against a
   repository's own `.patchfrog.yml` (read by hand) against
   `GLOBAL_PUBLICATION_ENABLED` (an environment variable, invisible
   from any CLI output).
4. **A genuinely clean review posts nothing at all, unconditionally** —
   confirmed by reading `PublicationPlanner.build_plan`'s `if not
   findings:` branch, which returns `SKIPPED_NO_FINDINGS` with no
   comment, and confirming no existing config/docs statement declares
   this a deliberate choice (it was simply unspecified behavior, exactly
   the "ambiguous" case the milestone spec anticipates). A clean PR
   during a beta could look identical to PatchFrog silently failing.
5. **A live-bug-class regression risk with no automated guard**: the
   real `thinking_level`/`thinking_budget` model-family bug fixed in
   Milestone H (provider=gemini, model silently defaulting to the
   Anthropic model name) had no doctor-style check that would catch the
   *next* instance of the same mistake before a beta operator hits it
   live.
6. **No `SECURITY.md` or `CONTRIBUTING.md`.**

**Deliberately not built** (per the spec's own "do not force this"
allowances): a `support bundle` command (documented as a manual
diagnostic workflow in `docs/beta-runbook.md` instead — existing
`doctor`/`ops failed`/`ops usage`/`telemetry review` already cover it);
a hosted dashboard of any kind; a repository-level allowlist beyond
GitHub's own App-installation repository selection plus the existing
`BETA_ALLOWLIST_MODE` (judged sufficient for a 3-5 repository beta).

## 2. Code changes

### `patchfrog ops doctor` (new, `patchfrog/ops/doctor.py`)

Comprehensive, secret-safe deployment diagnostic. PASS/WARN/FAIL per
check, exit code 0 (all pass/warn) / 1 (any FAIL) / 2 (internal doctor
failure, never a configuration problem). Catches `Settings()`'s
`ValidationError` and reports each missing field as its own actionable
line instead of crashing. Checks: settings completeness, deployed git
SHA, webhook secret presence and placeholder-value detection
(`change-me`), private key presence/shape, provider/credential
presence, **provider/model family sanity** (conservative
`gemini-`/`claude-` prefix check, after normalizing a `models/`-prefixed
resource-path form — directly targets the exact live bug class found in
Milestone H), operator hard caps (informational), publication gate
state (informational), webhook route/permissions (informational), and
an optional best-effort live `GET /app` auth check (never FAILs on
network trouble, since an isolated/offline doctor run should still be
useful). Never calls an LLM, never mutates state, never prints a secret
value (13 tests directly assert this, including one that constructs a
report with real-shaped secret strings and asserts none of them appear
in any check's rendered output).

### `patchfrog ops preflight --repository owner/repo` (new, `patchfrog/ops/preflight.py`)

Answers `PUBLISH` / `DRY_RUN` / `BLOCKED` for one repository, without
requiring a real webhook delivery first. Reuses
`patchfrog.ops.eligibility.check_eligibility` **directly** — the exact
function the real webhook-triggered pipeline calls — rather than a
second, potentially-diverging copy of eligibility logic. The one live
network step (resolving `.patchfrog.yml` from the repository's current
default branch, via a new, minimal `GitHubClient.get_default_branch_head_sha`
method) is best-effort: an unreachable GitHub API degrades that single
check to `WARN` and the gate is correctly treated as unresolved (never
silently assumed open) — proven by a dedicated test. Never calls an
LLM, never mutates state (9 tests, including a structural check that
`LLMProvider`/`generate_structured` never appear in the module's own
source).

### `patchfrog telemetry beta-summary --since 7d [--repository owner/repo]` (new, `patchfrog/telemetry/beta_summary.py`)

Read-only operator summary (runs total/succeeded/partial/failed,
findings published, provider calls/tokens, feedback coverage) over a
time window. Reuses `patchfrog.telemetry.aggregation`'s existing, already
-tested `aggregate_snapshots`/`compute_feedback_coverage` — no new
analytics subsystem, no composite score, feedback never mixed into
benchmark ground truth (unchanged from the existing telemetry design's
own strict separation).

### Zero-finding UX (`PublicationConfig.post_clean_summary`, off by default)

A genuinely clean review (Phase 5 produced zero findings — never the
"findings existed but were filtered/omitted/already-reported" case,
which is deliberately left unchanged) can now, opt-in per repository,
post a short, honest "PatchFrog found no publishable findings in this
review" summary instead of nothing at all
(`patchfrog.publishing.body.format_clean_review_body`). Off by default
— every existing deployment's behavior is byte-for-byte unchanged
unless a repository explicitly sets `publish.post_clean_summary: true`.
`PUBLICATION_CONFIG_SCHEMA_VERSION` bumped 2 → 3 (the new field is
folded into `PublicationConfig.fingerprint()`, exactly the same
precedent already set when `frog_marker` bumped it 1 → 2) —
**necessary, not mechanical**: publication identity must never let a
policy that now writes a real comment collide with one that wrote
nothing, for a *new* publication attempt. Never affects an
already-`PUBLISHED` row, which is permanently protected regardless by
the existing partial unique index + `ALREADY_PUBLISHED` short-circuit
before any fingerprint is even recomputed for that identity again — see
the field's own docstring and the config module's version-history
comment for the full reasoning. 5 new planner unit tests (including two
proving `post_clean_summary` never fires when findings were filtered/
suppressed rather than genuinely absent) + 2 new real end-to-end
publish tests (`FakeReviewPublisher`, one DRY_RUN, one an actual
simulated GitHub write).

### Docs fix: `docs/onboarding.md`'s gate count

"Two independent gates" → "three independent gates", the global switch
now explicitly enumerated alongside the two that were already there,
plus a pointer to `patchfrog ops preflight` for checking all three at
once.

## 3. New documentation

- `docs/external-beta.md` — the beta contract: self-hosted only, Cloud
  not yet available, explicit limitations, recommended initial rollout
  (3-5 repos, `BETA_ALLOWLIST_MODE=true`, Gemini as the more recently
  live-validated provider recommendation, not a requirement).
- `docs/quickstart.md` — the one canonical clone-to-first-review path
  (16 numbered steps as specified), replacing the previously-scattered
  README/deployment.md/onboarding.md pieces a fresh operator would
  otherwise have had to assemble themselves. README now links to it
  instead of duplicating.
- `docs/beta-runbook.md` — day-to-day operator playbook: invite a repo,
  doctor/preflight, enable publication, inspect failed/stale runs,
  inspect telemetry, sync feedback, suspend/disable at every level,
  uninstall/reinstall recovery (citing the exact already-passing tests
  that prove it), provider quota incident, webhook outage, worker
  outage, rollback, key/secret rotation. No secret ever appears in an
  example command or its output.
- `docs/beta-invite-checklist.md` — one-page per-repository checklist
  template, deliberately with no real repository name or contact
  filled in.
- `docs/privacy.md` — factual (not legal) summary of what the code
  actually persists/doesn't, consolidating (never duplicating wholesale)
  what `docs/feedback.md`, `docs/telemetry-intelligence.md`, and
  `docs/operations.md`'s "Data retention" already established, plus
  explicit non-claims (no third-party zero-retention claim, no
  encryption/compliance claim).
- `SECURITY.md` — private vulnerability reporting is **not currently
  enabled** on `kadireren7/patchfrog` (confirmed via `gh api
  repos/kadireren7/patchfrog/private-vulnerability-reporting` →
  `{"enabled":false}`, not assumed) — documented honestly as not yet
  available, with a minimal non-sensitive-issue fallback, rather than
  inventing an email address or claiming a channel that doesn't exist.
  **Operator action item, not fixed by this PR**: enable GitHub private
  vulnerability reporting on the real repository.
- `CONTRIBUTING.md` — license notice, dev setup, gates, explicit "no
  live LLM calls in the test suite" / "no secrets in fixtures" rules,
  pointer to `SECURITY.md`. No CLA system added (not required, per the
  milestone's own instruction).
- `docs/deployment.md` — added a `patchfrog ops doctor` pointer right
  next to the health-endpoints section it complements, explaining
  exactly why it exists (the raw-`ValidationError`-on-missing-config
  gap).
- `docs/operations.md` — `doctor`/`preflight`/`telemetry beta-summary`
  added to the CLI command list and the troubleshooting table.
- README — links to `docs/quickstart.md` and `docs/external-beta.md`;
  CLI table's `ops` row mentions doctor/preflight. Positioning language
  ("source-available", "PatchFrog Cloud (planned / under development)")
  was already correct — confirmed via re-read, not changed.

## 4. License/trademark consistency (re-audited, unchanged)

`grep -rniI "open.source"` across README, every `docs/*.md`, `LICENSE`,
`TRADEMARK.md`, and the two new root docs (`SECURITY.md`,
`CONTRIBUTING.md`) finds exactly one match: `docs/licensing.md`'s own
deliberate clarification ("is **not** an OSI-approved 'open source'
project"). No accidental "open-source" claim anywhere. No change made —
this was already correct from an earlier milestone
(see the PR #32 licensing/Cloud-boundary milestone).

## 5. Dogfooding the onboarding flow (real, read-only, no LLM)

Rather than a fresh dogfood PR (this milestone's own spec: "No need for
another live LLM dogfood" if a real read-only GitHub check suffices),
`doctor` and `preflight` were run against PatchFrog's own real,
already-configured deployment and real GitHub App/repository:

```
$ patchfrog ops doctor --no-github-check
[PASS] settings: all required variables present
[PASS] deployed_commit: e452940790014aac7b5edab9f1f4b8b5cf7155ad
[PASS] github_webhook_secret: present (length=64)
[PASS] github_private_key: source=GITHUB_PRIVATE_KEY_PATH, well-formed PEM shape
[PASS] webhook_route: ...
[PASS] publication_gates: GLOBAL_PUBLICATION_ENABLED=True GLOBAL_REVIEW_PROCESSING_ENABLED=True BETA_ALLOWLIST_MODE=False ...
[PASS] operator_hard_caps: max_candidates=100 max_total_input_tokens=1000000 ...
[PASS] review_provider: provider=anthropic model=claude-opus-5 critic_model=claude-opus-5
[PASS] review_provider_credential: ANTHROPIC_API_KEY present (length=108)
[PASS] model_family:PATCHFROG_REVIEW_MODEL: 'claude-opus-5' looks like a anthropic model name
[PASS] database: migration=0017_telemetry_intelligence
[PASS] redis:

overall: PASS
```

```
$ patchfrog ops preflight --repository kadireren7/patchfrog
[PASS] repository: known, installation_id=153810631, is_selected=True
[PASS] eligibility: review generation would run
[PASS] publish_gate:global: GLOBAL_PUBLICATION_ENABLED=True
[WARN] publish_gate:installation: publication_allowed=False
[WARN] publish_gate:repository: publish.enabled=False (min_severity=medium, resolved at e45294079001)

outcome: DRY_RUN
```

Both results are **correct and expected**: `installation.publication_allowed`
and the real repository's `main`-branch `.patchfrog.yml` were both
deliberately left in their reverted, non-publishing state after
Milestone H's own validation (see
Milestone H's own "Cleanup discipline" (validation/production_e2e/latest-summary.md)) — this
run independently *confirms* that reverted state is exactly what
`preflight` reports, live, against real GitHub data, with zero
mutation. A third check — pointing `preflight` at an unknown repository
name — correctly reported `BLOCKED` with exit code 1:

```
$ patchfrog ops preflight --repository nonexistent-owner/nonexistent-repo-xyz
[FAIL] repository: no repository known to PatchFrog under this name ...
outcome: BLOCKED
```

`patchfrog telemetry beta-summary --since 30d` was also run live against
the real database, correctly summarizing real historical review/
publication/feedback activity accumulated across every prior milestone's
own dogfooding (2,474 real review runs in the window, 8 real findings
published, real provider token totals, real feedback coverage) —
confirming the new command reads real accumulated state correctly, not
just fixture data.

**No LLM call was made by any of the above** — `doctor`'s and
`preflight`'s only network calls are to GitHub's REST API (App auth,
default-branch resolution, `.patchfrog.yml` read), never to Anthropic
or Gemini.

## 6. Test matrix

- `tests/integration/test_ops_doctor.py` (13 tests): missing-required-
  settings graceful FAIL (not a crash), all-good PASS, migration
  mismatch FAIL, Redis-unavailable FAIL, placeholder webhook secret
  WARN, missing provider credential WARN, Gemini-provider-with-unset-
  model family-mismatch WARN (the exact live bug class), matching
  family PASS, `models/`-prefixed name normalized before the family
  check, unsupported provider FAIL, no secret values ever in output,
  exit code semantics, structural no-mutation/no-LLM proof.
- `tests/integration/test_ops_preflight.py` (9 tests): unknown
  repository BLOCKED, global-processing-disabled BLOCKED, repository-
  not-selected BLOCKED, suspended-installation BLOCKED, beta-pending
  BLOCKED, all-publish-gates-closed DRY_RUN, unreachable `.patchfrog.yml`
  never assumed open (still DRY_RUN even with the other two gates
  open), all-gates-confirmed-open PUBLISH, structural no-mutation/no-
  LLM proof.
- `tests/integration/test_telemetry_beta_summary.py` (4 tests): real
  succeeded run counted correctly, repository-scoping never leaks
  another repository's runs, time-window exclusion, `parse_since`'s
  relative/ISO parsing.
- `tests/unit/test_publishing_planner.py` (+5 tests): clean-summary
  plan is publishable in both DRY_RUN and PUBLISH mode when enabled,
  never fires when findings were filtered/already-reported even with
  the setting on.
- `tests/integration/test_publishing_service_dry_run.py` (+1),
  `tests/integration/test_publishing_service_publish_e2e.py` (+1): the
  clean-summary review proven end-to-end through the real planner +
  service + `FakeReviewPublisher`, including an actual simulated
  GitHub write with zero inline comments and the honest body text.

No live LLM call anywhere in this test matrix.

## 7. Gates

- `git diff --check`: clean.
- `ruff check .`: clean.
- `mypy . --strict`: clean, 421 source files (418 + 3 new modules).
- `pytest`: see final report for the exact total; the same 3
  pre-existing, unrelated `test_static_analysis_service.py` failures
  persist (confirmed via `git stash` before this milestone's own work
  began, per the same discipline as every prior milestone) — never
  represented as 0 failures.
- Alembic: single head, unchanged (`0017_telemetry_intelligence`) — no
  migration needed (no persisted schema changed).
- Docker: both `api`/`worker` images build clean; 9/9 Celery tasks
  registered in the built worker image.
- Full-diff secret scan: clean.

## 8. What was NOT done

- No live LLM call (Gemini or Anthropic), anywhere, in this milestone.
- No Cloud/dashboard feature work.
- No new agent role, no new LLM provider, no prompt redesign, no
  benchmark tuning.
- No support-bundle command built (documented as a manual workflow
  instead, per the spec's own "do not force this" allowance).
- No repository-level allowlist beyond GitHub's own App-installation
  repository selection plus the existing `BETA_ALLOWLIST_MODE` — judged
  sufficient for a 3-5 repository beta, not redesigned.
- GitHub private vulnerability reporting was **not** enabled on the
  real repository by this PR — that is a one-click operator action
  documented in `SECURITY.md`, not something this PR's diff can do on
  the maintainer's behalf.
