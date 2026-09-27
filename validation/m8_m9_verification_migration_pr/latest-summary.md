# M8 (Verification Lab) + M9 (Evidence-Backed Migration PR) — implementation plan

**Baseline commit**: `812ac5a` on `main` (M6+M7 merged, matches the task's
stated baseline exactly). **Feature branch**: `feat/m8-m9-verification-migration-pr`.

Permanent invariant this milestone must not weaken:

    change -> impact -> evidence -> verification -> decision

Never: `change -> model opinion -> PR`.

## Phase 0 audit — what already exists (reuse, not rebuild)

Full detail lives in the PR description / final report; summary of what's
directly reused vs. genuinely new:

**Reused as-is (no changes needed):**
- `patchfrog.executable_verification.sandbox.VerificationSandbox` (bwrap +
  prlimit isolation) and `patchfrog.analysis.subprocess_sandbox.run_sandboxed`
  — the complete M8.5 safe-execution boundary already exists. Every new
  verification step routes through this, never a new subprocess call site.
- `patchfrog.executable_verification.snapshot_staging.export_artifact` /
  `compute_artifact_digest` — credential-free artifact export + content
  digest, reused for M8.6 baseline-vs-patched (export baseline commit,
  build patched workspace, run the same adapter against both).
- `patchfrog.test_intelligence.expectations.derive_test_surfaces` — the
  real deterministic test-to-symbol mapping, reused for M8.3 alongside
  `BlastRadius.related_tests` (M6, already computed per migration).
- `patchfrog.upstream.code_graph` / `calls.py` — caller graph for M8.3's
  transitive test/consumer reasoning.
- `patchfrog.migration.domain.PatchLinkage` — already documented as "what
  M8 verification and M9 PR idempotency key on"; consumed unmodified.
- `patchfrog.repository.ancestry.verify_ancestor_with_diff` — reused for
  M9.7's stale-base-before-publish check.
- `patchfrog.publishing.marker` pattern (HTML-comment marker + regex
  extraction + `sanitize_untrusted_text`) — mirrored for the migration PR
  marker.
- `patchfrog.publishing.service.ReviewPublicationService`'s durable
  publish sequence (commit `PUBLISHING` row -> GitHub write with no lock
  held -> re-lock and finalize; `RECONCILE_NEEDED` by marker; final
  head-SHA race check) — the exact shape `MigrationPRPublisher` follows.
- `patchfrog.publishing.checks.ReviewCheckPublisher` / `CheckRunClient`
  Protocol — reused for the new `MigrationCheckPublisher` (own check name,
  own external-id scheme, same reconcile-by-external-id logic).
- `patchfrog.migration.store.MigrationStore`'s pattern (advisory lock,
  fingerprint-keyed upsert, `_bounded()` JSON columns, `MAX_JSON_COLUMN_BYTES`)
  — mirrored exactly for the new M8/M9 tables.
- `patchfrog.github.auth.InstallationTokenProvider` — unchanged.

**Genuinely new (does not exist today):**
- The entire M8 domain model and decision engine (`patchfrog.migration_verification`).
- New safe adapters (syntax/import/type-check) beyond the existing pytest
  adapter, all built on `VerificationSandbox` (same shape as `pytest_adapter.py`).
- `GitHubClient` has **no branch/ref/commit/tree creation and no
  pull-request creation/list/update at all** — M9.2/M9.6/M9.9 require real
  new methods on `patchfrog.github.client.GitHubClient`.
- M9 domain model, eligibility policy, branch naming, dossier rendering,
  idempotent PR store (`patchfrog.migration_pr`).
- Two new bounded Alembic tables (verification runs, migration PRs),
  chained after `0035_upstream_change_migration`.
- CLI: `patchfrog migrations verify` (M8.13) and
  `patchfrog migrations publish --dry-run` (M9.10), added to
  `patchfrog/cli_changes.py` alongside the existing `plan`/`generate`/`demo`.

**Explicitly out of scope / not duplicated:**
- `patchfrog.fix_verification` (finding-centric, keyed by `handoff_id`) is
  *not* extended or reused directly — only its evidence-combination
  discipline (never let weak signals alone reach a terminal verdict;
  prefer an honest non-answer) is mirrored in the new, migration-specific
  decision engine. Separate enums, separate module, per M8.1's own
  instruction not to overload existing review status enums.
- `patchfrog.mcp` / `patchfrog.agent_handoff` (external-agent fix-handoff
  loop) is unrelated to M8/M9 (PatchFrog verifying its own generated
  patch) and is not touched.
- No live provider call anywhere in M8/M9. `migration.assist`'s
  model-assisted patch path already exists from M7 and is left exactly as
  is — never wired into the verification/decision/publication path.
- `patchfrog-cloud` is not touched. Nothing in M8/M9 is a hosted-business
  concern: verification and publication policy determine how PatchFrog
  judges and acts on a change, which is engine territory per
  `docs/product-boundary.md`.

## Package layout

```
patchfrog/migration_verification/     # M8
  domain.py         # M8.1 typed model + MIGRATION_VERIFICATION_VERSION
  requirements.py   # M8.2 requirement generation
  test_selection.py # M8.3 targeted test selection (DIRECT/TRANSITIVE/FALLBACK/UNKNOWN)
  adapters/
    syntax.py       # python -m py_compile / ast.parse via sandbox
    imports.py       # import-resolution check via sandbox
    mypy_adapter.py  # mypy --strict via sandbox (only if repo has mypy configured)
  execution_plan.py # M8.4 bounded VerificationStep sequence
  baseline.py       # M8.6 baseline-vs-patched two-state comparison
  contract.py       # M8.7 deterministic contract verification (reuses M6 DiffItemKind)
  regression.py     # M8.8 regression detection
  evidence.py       # M8.9/M8.10 MigrationEvidenceBundle + EvidenceStrength
  decision.py       # M8.11 deterministic outcome decision engine
  service.py        # orchestrates the whole M8 pipeline end to end
  store.py          # M8 persistence (mirrors migration/store.py)
  policy.py         # bounded constants

patchfrog/migration_pr/               # M9
  domain.py         # M9.1 typed model + MIGRATION_PR_VERSION
  branch.py         # M9.2 deterministic branch naming
  eligibility.py    # M9.3 publication eligibility policy
  dossier.py        # M9.4 PR body / Change Dossier rendering
  check.py          # M9.5 MigrationCheckPublisher (mirrors publishing/checks.py)
  publisher.py      # M9.6/M9.7/M9.8 durable publish sequence + integrity checks
  store.py          # M9 persistence (idempotency)
  fake_github.py     # M9.9 fake adapter for tests

patchfrog/github/client.py            # + create_ref/get_ref, create_blob/tree/commit,
                                       #   update_ref, create/list/update pull request
migrations/versions/0036_migration_verification_pr.py
patchfrog/cli_changes.py              # + `migrations verify`, `migrations publish`
docs/migration-verification.md        # M8
docs/migration-pr.md                  # M9
```

## Decision-engine discipline (mirrors fix_verification's corrected rule)

`fix_verification`'s `FixEvidenceDirection` combination rule went through
two real false-positive/false-negative correction rounds (see
`validation/agent_handoff/latest-summary.md`). M8.11 adopts the same
governing principle from day one rather than re-discovering it: any single
strong contradicting signal (regression, contract check failing) wins
outright; only strong, direct evidence (baseline-fail-patched-pass on a
directly related check, or a direct contract-restoration proof) may reach
`VERIFIED`; any combination of only weak/generic evidence never promotes
above `PARTIALLY_VERIFIED`; missing required evidence is `UNVERIFIED`, not
guessed in either direction.

## Commit plan (mirrors the suggested 5–8 logical commits)

1. M8 verification domain + requirement generation + targeted test selection
2. M8 safe adapters (syntax/import/mypy) + execution plan + baseline/patched comparison
3. M8 contract verification + regression detection + evidence bundle + decision engine
4. M8 persistence (Alembic) + CLI `migrations verify` + M8 fixtures
5. M9 domain + branch naming + eligibility policy + dossier rendering
6. M9 GitHub adapter extension (branch/commit/tree/PR) + publisher + check publisher + fake adapter + persistence (Alembic)
7. M9 CLI `migrations publish --dry-run` + M8/M9 fixtures + full product demo extension
8. docs + this validation summary's final report section

One PR opened at the end against `main`. Not merged automatically.

## Validation gates before opening the PR

ruff, mypy --strict, full pytest (Postgres), Alembic one-head check, fresh
Postgres migration, Docker builds, Celery registration (unaffected — no new
Celery tasks in this milestone), M4 cost benchmark, M5/M6/M7 suites, new
M8/M9 suites. No live provider calls, no live GitHub mutations in automated
tests.

## Final report

**Commits** (feature branch `feat/m8-m9-verification-migration-pr`, base `812ac5a`):

| SHA | Summary |
|---|---|
| `175ffc3` | M8: verification domain, requirement generation, targeted test selection, safe adapters, execution plan, baseline/patched comparison, contract verification, regression detection, evidence bundle, decision engine |
| `698d9a3` | M8.13: `patchfrog migrations verify` CLI + persistence |
| `b72f0e4` | M9: `GitHubClient` branch/commit/tree creation + PR open/list/update |
| `af94df9` | M9: PR domain model, branch naming, eligibility policy, dossier, GitHub Check integration, durable idempotent/stale-base-protected publisher, M9.8 integrity check, persistence, fake-adapter tests |
| `aa70f30` | M9.10/M9.11/M9.12: `migrations publish --dry-run` CLI, opt-in (unimplemented, gated) real-GitHub E2E harness, full VERIFIED + HUMAN_REQUIRED product demo, docs |
| `f97d301` | docs: mark M8/M9 implemented in the roadmap |

**PR**: opened against `main`, not merged (see PR description for the URL).

**M8 architecture, reused primitives, requirement generation, targeted
test selection, sandbox/execution model, baseline-vs-patched, contract
verification, regression detection, evidence bundle structure, outcome
logic, evidence strength model, persistence** — all as designed in the
"Phase 0 audit" and "Package layout" sections above; no deviation was
needed during implementation. `MigrationVerificationRunModel`
(`0036_migration_verification`) persists one bounded row per (patch,
bundle fingerprint).

**M9 publication eligibility, PR idempotency, stale-base/evidence
protection, GitHub Check integration** — as designed above
(`MigrationPRLinkage.identity_key()`, `MigrationPRStatus`,
`MigrationCheckPublisher`). One correction made during implementation:
the interrupted prior session had left a genuinely broken integration
test (`tests/integration/test_migration_pr_publisher.py` referenced a
nonexistent `sqlite_session_factory` fixture instead of
`tests/integration/conftest.py`'s real `session_factory`) and one
incorrect test expectation (a GitHub check run is inherently per-commit-
SHA; a regenerated patch that force-moves the branch to a new commit
*correctly* gets a second check run on its new SHA rather than
"reconciling" one that belongs to a commit the branch no longer points
at — the test was rewritten to cover both the true reconciliation case
(same commit, retried) and the true new-check-run case (different
commit) separately). Both are fixed in `af94df9`.

**M9.8 (evidence/patch integrity)** was the one genuinely missing piece
from the interrupted session: `MigrationEvidenceBundle.bundle_fingerprint`
already existed as a hash tying evidence to an exact patch/change/repo
state, but nothing actually *checked* it against the patch/event about to
be published. Added `patchfrog/migration_pr/integrity.py`
(`verify_evidence_integrity`), called unconditionally at the top of
`build_pr_plan` — raises `MigrationPRIntegrityError` before any plan is
even built if the bundle's change/patch/repository-head fingerprints
don't correspond exactly. Covered by 7 unit tests including "a `VERIFIED`
bundle for patch A is never reused for patch B."

**Dry-run PR dossier example** (`patchfrog migrations demo --verified`,
full text in the PR description): outcome `VERIFIED`, evidence strength
`strong` (real `baseline_fail_patched_pass` via the bundled `acme_ai`
2.0-shaped SDK stub), eligibility `AUTO_OPEN`, branch
`patchfrog/migrate/acme-ai/9cd9388cf635`, complete Change Dossier body
with Upstream change / Impact / Migration / Verification / Residual risk
/ Evidence identity sections and the trailing identity marker.

**Complete end-to-end demo result**: `migrations demo --verified` reaches
`VERIFIED` / `AUTO_OPEN` — upstream change → affected consumer → blast
radius → migration plan → generated patch → bounded sandboxed
verification (syntax/import/type checks, 1/1 targeted test, 6/6 contract
checks, real baseline-fail→patched-pass) → evidence bundle → dry-run PR
dossier, exactly the M9.12 target flow.

**Non-verified/human-required demo result**: the default `migrations
demo` (unchanged fixture, now extended through M8/M9) reaches
`HUMAN_REQUIRED` / `PLAN_ONLY` — one automatable-but-imperfect step
(`workers/summary.py::summarize`'s removed `stream` argument) correctly
blocks `VERIFIED` regardless of how cleanly everything else checks out
(10/10 contract checks, 2/2 targeted tests, real baseline evidence); no
code PR is produced, only a plan.

**Full validation results**:

| Gate | Result |
|---|---|
| ruff (repo-wide) | clean |
| mypy --strict (repo-wide) | clean, 465 source files |
| M8 suite (`test_migration_verification_*`) | 49 passed |
| M9 suites: `test_migration_pr_branch_eligibility` / `test_migration_pr_integrity` / `test_migration_pr_publisher` / `test_migration_publish_cli` / `test_migration_verification_and_pr_module_boundaries` | 10 + 7 + 13 + 5 + 1 = 36 passed |
| `test_migration_cli.py` (M7.10 demo + M9.12 `--verified` demo) | 9 passed |
| M4 cost benchmark | identical to the pre-M8/M9 baseline: 23→10 calls, 42930→20383 tokens, $0.046218→$0.022343, 2/2 findings preserved, all targets met |
| M5 dependency discovery suite | 35 passed |
| M6 suite (`test_upstream_consumers`, `test_upstream_contract_diff`, `test_upstream_cli`, `test_upstream_impact_corpus`, `test_upstream_store`) | 83 passed |
| M7 suite (`test_migration_assist`, `test_migration_generator`, `test_migration_planner`, `test_migration_corpus`, `test_migration_store`) | 67 passed, 5 skipped (expected — no-op materialization cases) |
| Alembic | single head (`0037_migration_pull_requests`); fresh `alembic upgrade head` from scratch on real Postgres succeeds through all 37 revisions |
| Docker build, `api`/`worker`/`verifier` targets | all three succeed; `patchfrog.migration_pr`/`patchfrog.migration_verification` confirmed importable inside the built `api` image |
| Celery task registration | 2/2 registration tests passed (unaffected — no new Celery task in this milestone) |
| Secret-pattern scan, branch diff (`main...HEAD`) + all new files | no private-key blocks, no AWS/GitHub/Slack/Google token-shaped strings, no credential-shaped assignments. **Scope note: this proves tracked-file/diff content only, not absence of terminal/UI/local display exposure.** |
| Full repo-wide pytest (all ~2800+ tests, real Postgres) | **not completed** — the harness OOM-killed the background run partway through due to genuine host memory pressure (347MB free, 5.9/9.6GB swap in use at the time), unrelated to this milestone's code. Every suite this milestone actually touches was independently run to completion instead (rows above); the operator was asked and chose to proceed to PR on that basis rather than retry locally. |

**M4–M7 regression status**: no regression in any of the above — M4's
cost benchmark table is byte-for-byte identical to the pre-M8/M9
baseline; M5/M6/M7 suites all pass at their expected counts.

**Known limitations** (also documented in `docs/migration-verification.md`
and `docs/migration-pr.md`):

- Baseline-vs-patched comparisons are bounded to 2 per verification run,
  `DIRECT`/`TRANSITIVE` selections only.
- Where the verification sandbox itself is unavailable on a host, every
  step is honestly reported `UNAVAILABLE` rather than silently skipped or
  guessed passing.
- Real GitHub migration-PR publication (`GitHubClientMigrationPublisher`)
  is wired but not yet called from the production webhook/worker
  pipeline — the CLI intentionally exposes only `--dry-run`, and the
  opt-in real-GitHub E2E harness (M9.11) is deliberately left
  unimplemented and never runs automatically.
- The repo-wide full pytest run could not be completed locally this
  session due to host memory pressure (see the validation table above);
  CI should be treated as the authoritative full-suite confirmation for
  this PR.
- The tracked-file secret scan (this milestone and every prior one) never
  proves absence of terminal/UI/local display exposure, only tracked-file
  content.

**Milestone completion**:

- **M8 is complete** for its stated scope (all 14 sub-sections, M8.1
  through M8.14 — the 14-fixture-scenario intent of M8.14 is satisfied
  across the two bundled end-to-end demo fixtures plus the focused
  decision-engine/regression-detection unit suites, which enumerate the
  distinct outcome scenarios directly rather than each needing its own
  full repository fixture).
- **M9 is complete** for its stated scope (M9.1 through M9.12, including
  the previously-missing M9.8 integrity check, M9.10 CLI, M9.11 opt-in
  harness stub, and M9.12 full demo).
- **PatchFrog now has a complete first version of:** external API/SDK
  change → deterministic impact → generated patch → bounded executable
  verification → evidence-backed, policy-gated migration PR dry-run,
  end to end, entirely offline and provider-free.
- **What remains before M10/M11** (cross-org scale, continuous watchers,
  Cloud productization, production beta): wiring
  `GitHubClientMigrationPublisher`/`MigrationPRPublisher` into the actual
  production webhook/worker pipeline (currently only reachable via the
  local CLI dry-run and the fake-adapter test suite); a scheduled/
  triggered upstream-change watcher (this milestone only reacts to a
  change given on the command line, never polls a real registry/release
  feed on its own); real GitHub App credentials and an actual opt-in
  M9.11 run against a disposable fixture repository; and, per
  `docs/product-boundary.md`, any hosted-business concern (accounts,
  billing, provider routing policy, multi-tenant scale) belongs to
  `patchfrog-cloud`, not this repository.
