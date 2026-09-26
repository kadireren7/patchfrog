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
