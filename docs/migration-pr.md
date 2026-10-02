# Evidence-Backed Automated Migration PR (M9)

`patchfrog/migration_pr/` is the final stage of **change -> impact ->
evidence -> verification -> decision**: given M8's evidence bundle,
**may PatchFrog open a PR at all, and if so, what exactly should it
contain?** `MIGRATION_PR_VERSION = 1`. No LLM ever decides eligibility,
branch identity, or PR content -- every field of a `MigrationPRPlan` is
derived from already-computed M6/M7/M8 evidence. This is specifically an
upstream-dependency-compatibility migration PR workflow, not a generic
coding-agent PR workflow.

```bash
# Local dry-run: the exact branch/commit/PR/check plan a real publish
# would use, without ever touching GitHub.
python -m patchfrog.cli migrations publish --dry-run \
  --old old.yaml --new new.yaml --hints hints.yaml \
  --repo demo=path/to/repo --repository my-org/my-repo

# The bundled demo, end to end through the dry-run PR dossier
python -m patchfrog.cli migrations demo --verified
```

Real publication (an actual GitHub write) requires a GitHub App
installation and is intentionally not reachable from this CLI in this
milestone -- see "Known limitations" below.

## Domain model (`domain.py`)

`MigrationPRLinkage` is the migration-PR analogue of M7's `PatchLinkage`:
upstream change fingerprint, dependency key, repository, base commit SHA,
plan/patch/verification-plan/bundle fingerprints. Its `identity_key()` is
deliberately narrower than the full linkage -- repository + change
fingerprint + engine version only -- so a *legitimately regenerated*
patch for the same upstream change reconciles the same PR (M9.6) rather
than creating a duplicate one.

`MigrationPRPlan` is the complete, deterministic output of the
publication planner (`planner.py`): everything a dry-run needs to print,
and everything a real publish needs to write, with no further decisions
left to make at write time -- mirrors
`patchfrog.publishing.domain.ReviewPublicationPlan`'s own role for review
publishing.

## Eligibility policy (M9.3, `eligibility.py`)

A pure function of the M8 `VerificationOutcome` and an operator-controlled
`MigrationPRPolicy`. Never silently relaxed:

| Verification outcome | Eligibility |
|---|---|
| `VERIFIED` | `AUTO_OPEN` |
| `PARTIALLY_VERIFIED` | `OPEN_WITH_OPERATOR_APPROVAL` only if `policy.allow_partially_verified` (off by default); otherwise `NOT_ELIGIBLE` |
| `HUMAN_REQUIRED` | `PLAN_ONLY` -- a report may exist, never an automatic code PR |
| `UNVERIFIED` / `FAILED` / `REGRESSION_DETECTED` | `NOT_ELIGIBLE`, unconditionally -- no policy knob relaxes these |

A `PARTIALLY_VERIFIED` PR that is opened is always visibly marked in the
dossier (`dossier.py`'s own banner) -- the bar is never lowered silently.

## Branch naming (M9.2, `branch.py`)

```
patchfrog/migrate/<provider>/<change-fingerprint-short-id>
```

Stable for the same migration identity (a re-run reconciles the same
branch, never a new one), sanitized to a git-ref-legal token, and falls
back to a stable `dependency` segment when no explicit provider identity
exists -- the branch shape is always exactly three path segments. The
branch name is only ever used as a JSON field value in a GitHub API
request body (`patchfrog.github.client`), never shell-interpolated, so
there is no command-injection surface.

## Change Dossier (M9.4, `dossier.py`)

A concise, evidence-backed PR body -- never raw logs, never AI prose.
Sections: Upstream change, Impact (blast radius summary), Migration
(strategies applied, files changed, human-required steps), Verification
(final outcome, targeted tests passed, contract checks, type check,
baseline-vs-patched evidence, evidence strength), Residual risk, and
Evidence identity (every fingerprint plus the repository base SHA). Ends
with an invisible HTML-comment marker (`marker.py`) carrying the
migration's `identity_key()` -- the same "sanitize untrusted text before
interpolation" discipline `patchfrog.publishing.marker` already uses,
even though dossier content here is deterministic evidence, not free-form
AI output.

## GitHub Check integration (M9.5, `check.py`)

Reuses `patchfrog.publishing.checks.CheckRunClient`'s Protocol and
`GitHubClient` verbatim -- a second check *name*
(`"PatchFrog Migration Verification"`) and external-id scheme, never a
second check-run publishing mechanism. `MigrationCheckPublisher.reconcile`
looks up an existing check run by name + external ID *for the given head
SHA* before creating a new one -- and since a GitHub check run always
lives on one exact commit, a regenerated patch (a new commit, because the
branch was force-moved) legitimately gets its own check run on its own
SHA; reconciliation only fires for a genuine retry of the *same* commit.

## Publication sequence (M9.6/M9.7/M9.8, `publisher.py`)

```
MigrationPRPlan -> eligibility gate -> idempotency lookup (identity_key)
  -> [DRY_RUN: stop here, no write]
  -> stale-base check (M9.7) -> integrity check (M9.8, already enforced
     earlier by the planner -- see below)
  -> branch create/update -> PR open/update (M9.6) -> check run
```

Migration branches are entirely PatchFrog-owned: every publish rebuilds
the branch fresh from the *current* base commit and force-moves it --
never a three-way merge, never preserving a human's own commits on it,
since nothing but PatchFrog's own generated content is ever expected to
live there. A durable row is written *before* any GitHub write is
attempted and re-locked after, mirroring
`patchfrog.publishing.service.ReviewPublicationService`'s own discipline
(GitHub and PatchFrog's database cannot share one transaction).

### Idempotency (M9.6)

One row per `identity_key` (`store.py`). Republishing the identical patch
content updates the same PR (never a duplicate); a regenerated patch
force-moves the same branch and updates the same PR; a PR closed by a
human is never reopened automatically (`NO_OP_UNCHANGED`).

### Stale-base protection (M9.7)

Before any write, the publisher reads the base branch's current head SHA
from GitHub and requires an *exact* match against
`linkage.base_commit_sha`. Any mismatch returns
`STALE_REQUIRES_REGENERATION` -- a moved base means the plan/patch/
evidence were computed against a repository state that no longer exists;
PatchFrog never publishes a stale `VERIFIED` status against a different
base.

### Evidence/patch integrity (M9.8, `integrity.py`)

Enforced even earlier than publication -- `planner.build_pr_plan` calls
`verify_evidence_integrity` before it builds anything, raising
`MigrationPRIntegrityError` if the evidence bundle's own
`change_fingerprint`/`patch_fingerprint`/`repository_head_sha` do not
correspond exactly to the change/patch/base-commit-SHA the plan is about
to be built for. A `VERIFIED` bundle computed for one patch can therefore
never be silently reused for a different one -- a caller passing
mismatched arguments gets a loud failure, not a quietly wrong plan.

## Testing (M9.9, `fake_github.py`)

`FakeMigrationGitHubPublisher` satisfies the `MigrationGitHubPublisher`
Protocol structurally -- a legitimate stand-in for tests, not a mock of
internal plumbing, and no real GitHub PR is ever opened by an automated
test. `tests/integration/test_migration_pr_publisher.py` covers: dry-run
makes no GitHub call, a verified migration opens a PR, republishing
identical/regenerated patches reconciles the same PR, stale base never
publishes, failed/regression/unverified outcomes never open a PR,
partially-verified respects operator policy, human-required never
touches GitHub, a closed PR is never reopened, and check-run
reconciliation is per-commit-SHA (a regenerated patch's new commit
legitimately gets its own check run; a retried identical commit
reconciles the existing one).

## Local dry-run (M9.10, `report.py`, `patchfrog migrations publish`)

Builds the exact `MigrationPRPlan` a real publish would use and renders
it -- proposed branch, commit title, PR title/body, check title/summary,
and whether policy allows publication -- without ever calling GitHub. A
migration PR targets exactly one repository, so `publish` takes exactly
one `--repo NAME=PATH` plus `--repository OWNER/REPO` (the GitHub
identity to publish against); `--allow-partially-verified` maps directly
to `MigrationPRPolicy.allow_partially_verified`.

## Persistence (`store.py`, migration `0037_migration_pull_requests`)

`migration_pull_requests` -- one bounded row per `identity_key`. Chained
after `0036_migration_verification`. Never a duplicate row for the same
upstream change; a repeated publish always updates the existing row's
patch/verification/PR-state fields.

## Known limitations

- Real GitHub publication (an actual branch/commit/PR write) is only
  exercised in this milestone against the fake adapter in tests; the CLI
  intentionally only exposes `--dry-run`. `GitHubClientMigrationPublisher`
  (`github_publisher.py`) already wires `MigrationPRPublisher` to a real
  installation-scoped `GitHubClient` for a future production caller;
  wiring that into the webhook/worker pipeline is future work. An opt-in
  real-GitHub E2E harness exists (M9.11,
  `tests/integration/test_migration_pr_real_github_e2e.py`, gated on
  `PATCHFROG_MIGRATION_PR_REAL_GITHUB_E2E=1`) but is deliberately left
  unimplemented and is never run in this milestone or in CI.
- Stale-base protection only checks the base branch's head SHA at
  publish time -- a base that moves *during* the publish sequence itself
  (between the ref read and the tree/commit write) is not separately
  re-checked; GitHub's own ref-update semantics (an unforced `update_ref`
  would fail on a non-fast-forward) are the last line of defense there.
- The dossier's blast-radius section summarizes per-dependency counts
  only; the full per-symbol detail lives in the M6 blast radius object
  itself, not duplicated into the PR body (deliberately, to keep the
  dossier concise per M9.4's own instruction).

## Deliberately out of scope

A generic coding-agent PR workflow of any kind; auto-merging the
generated migration PR (never done, regardless of verification outcome);
hosted-business concerns (accounts, billing, provider routing policy,
GitHub App identity) -- those belong to PatchFrog Cloud, never this
package, per `docs/product-boundary.md`.
