# Beta Runbook

Day-to-day operator playbook for running PatchFrog's external beta. See
`docs/quickstart.md` for first-time setup and `docs/external-beta.md`
for what the beta actually is. Every command below is read-only unless
explicitly marked otherwise, and none ever prints a secret value.

## Invite a new repository

1. Have the repository owner install the GitHub App on that repository
   (or add it to an existing installation's repository selection).
2. Confirm the installation exists and is in the state you expect:
   ```
   patchfrog ops installations
   ```
   If `BETA_ALLOWLIST_MODE=true` (recommended for a small beta -- see
   `docs/external-beta.md`), a new installation starts `beta_state=pending`.
   Activate it explicitly:
   ```
   patchfrog ops installations --activate <github_installation_id>
   ```
3. Run the comprehensive deployment diagnostic once more (cheap, and
   catches drift since your last check):
   ```
   patchfrog ops doctor
   ```
4. Run the per-repository preflight check:
   ```
   patchfrog ops preflight --repository <owner>/<repo>
   ```
   Expect `DRY_RUN` at this point (publication gates start closed) --
   `BLOCKED` means review generation itself won't even run; see the
   `eligibility` line's detail for exactly why.
5. Decide whether to enable publication for this repository yet (see
   "Optionally enable publication" below), or leave it `DRY_RUN` while
   you validate review quality first.
6. Walk through `docs/beta-invite-checklist.md` for this specific
   repository and keep it (a copy, not the committed template) as your
   record.

## Verify an installation

```
patchfrog ops installations
```

Shows every installation's `status`, `beta_state`, and
`publication_allowed`. Per-installation usage over the last 24h:

```
patchfrog ops usage
```

## Run doctor

```
patchfrog ops doctor
```

Comprehensive, secret-safe. `FAIL` blocks a real review from ever
succeeding; `WARN` is worth reading but not necessarily blocking (e.g.
no provider credential configured yet). Exit code 0 for
all-pass-or-warn, 1 for any `FAIL`, 2 for an internal doctor failure
(never a configuration problem).

## Run repo preflight

```
patchfrog ops preflight --repository <owner>/<repo>
```

Answers `PUBLISH` / `DRY_RUN` / `BLOCKED` for this exact repository,
right now, without needing a real PR. Never calls an LLM; the one live
network step (reading the repository's current `.patchfrog.yml`) is
best-effort -- an unreachable GitHub API degrades that one check to
`WARN`, never silently assumes the gate is open.

**Gates only, not provider health**: `PUBLISH` here means the
repository/eligibility/publication gates permit publication -- it never
re-checks provider/model/credential health or GitHub App auth (that's
`patchfrog ops doctor`'s job). Run both before considering a repository
beta-ready; a `PUBLISH` preflight with an unhealthy `doctor` report
still won't produce a real review.

## Allow review eligibility

Review *generation* (not publication) is normally automatic once an
installation is active and a repository is selected. If it's blocked,
`patchfrog ops preflight`'s `eligibility` check names the exact reason
(`patchfrog.ops.eligibility.IneligibilityReason`) -- act on that
directly (e.g. `--activate` a pending installation, or confirm the
repository wasn't deselected in GitHub).

## Optionally enable publication

All three gates, in order of how rarely you'll touch them:

```
# 1. Deployment-wide (env var, restart to change) -- usually left true.
GLOBAL_PUBLICATION_ENABLED=true

# 2. Per-installation (DB, no restart):
patchfrog ops installations --installation <id> --allow-publication

# 3. Per-repository (the repository owner's own .patchfrog.yml):
publish:
  enabled: true
  min_severity: medium
```

Re-run `patchfrog ops preflight --repository <owner>/<repo>` afterward
and confirm it reports `PUBLISH` before considering the repository
beta-ready.

## Inspect failed/stale runs

```
patchfrog ops failed [--since ISO]     # failed review runs, with error detail
patchfrog ops stale [--recover]        # runs stuck RUNNING past the threshold
patchfrog ops retry <review_run_id>    # re-enqueue the pipeline for that commit
```

`stale --recover` only ever marks local run state `FAILED` -- it never
publishes, retries, or touches GitHub.

## Inspect telemetry

```
patchfrog telemetry review <review_run_id> [--format json]
patchfrog telemetry beta-summary --since 7d [--repository owner/repo]
```

`beta-summary` answers the operator-level questions across a time
window: how many reviews ran, how many succeeded/failed, findings
published, provider calls/tokens, and feedback coverage -- read-only,
reuses the existing telemetry aggregation, never a new analytics store.
Its query cost scales with the number of runs in the window (one
collector call per run, each already query-bound) -- fine for a
handful of repositories and tens of runs a week; prefer a narrower
`--since` window or `--repository` filter if a beta ever grows well
beyond that scale.

## Sync feedback

```
patchfrog feedback sync --repository <owner>/<repo> --pr <number>
```

Poll-only (see `docs/feedback.md`) -- run it after a beta user has had
a chance to react to or reply on a published finding. Never
webhook-driven.

## Suspend a repository

The App's own repository selection is the primary lever -- deselect the
repository from the installation (in GitHub's UI, or via the
`installation_repositories` "removed" event, which PatchFrog reacts to
automatically): `RepositoryModel.is_selected` flips `False` and no new
review work is ever scheduled for it again, checked before every
pipeline stage.

## Disable publication for one installation

```
patchfrog ops installations --installation <id> --disallow-publication
```

Review generation keeps running (candidates still get indexed/analyzed/
reviewed); only the publish stage stops writing to GitHub for that
installation.

## Suspend an installation entirely

```
patchfrog ops installations --installation <id> --suspend
```

Every repository under that installation stops being eligible
immediately -- no per-repository edits needed.

## Disable all beta activity (emergency)

Deployment-wide kill switches (env vars, restart required):

```
GLOBAL_REVIEW_PROCESSING_ENABLED=false   # stop all new indexing/analysis/AI review
GLOBAL_PUBLICATION_ENABLED=false         # stop all new GitHub writes (existing in-flight work still completes review, just never publishes)
```

Intentionally the coarse, rarely-touched emergency stop -- see
`docs/operations.md`'s "Kill switches" for the full comparison against
the per-installation/per-repository levers above, which never require a
restart and are the normal day-to-day tool.

## Uninstall / reinstall recovery

- **Repository removed from an installation** (`installation_repositories`
  "removed"): `is_selected` flips `False` automatically -- no manual DB
  edit, fails closed on the next PR.
- **Installation suspended/deleted**: `InstallationModel.status` updates
  automatically -- every repository under it stops being eligible
  immediately (see `tests/integration/test_installation_sync.py`'s
  `test_deleted_event_on_a_never_before_seen_installation_self_heals_then_marks_deleted`
  and `test_suspend_then_unsuspend_flips_status_on_the_existing_row` for
  the exact, already-tested behavior).
- **Reinstalled later** (same account, new or same installation id): the
  `installation` `created` webhook event self-heals the row -- no manual
  recovery needed (see
  `test_created_event_self_heals_a_new_installation_row`). If
  `BETA_ALLOWLIST_MODE=true`, the reinstalled installation starts
  `pending` again and needs `--activate`, same as any new one.
- **Historical data**: review runs, findings, publications, and
  feedback for a removed/uninstalled repository are never deleted --
  they remain queryable (`patchfrog ops failed`, `patchfrog telemetry
  review`) for audit, even though no new work is ever scheduled.

## Provider quota / rate-limit incident

Symptom: `patchfrog ops failed` shows a run failed with
`PROVIDER_RATE_LIMIT` or `PROVIDER_ERROR` (see `docs/operations.md`'s
"Error taxonomy"). Rate-limit failures are already retried with bounded
backoff automatically -- no action needed unless they persist across
every retry, which signals a real quota exhaustion (see
`docs/deployment.md`'s Gemini free-tier note: as low as 20 requests/day
was observed live). If persistent: switch provider/model, request a
quota increase from the provider, or wait out the window. Never retry
manually in a tight loop.

## Webhook outage

Symptom: PRs open with no corresponding activity at all in worker logs.
Check:

1. `GET /health/ready` -- API process itself healthy?
2. The GitHub App's own "Recent Deliveries" page (its settings ->
   "Advanced") -- did GitHub even attempt delivery, and what response
   code did it get back?
3. `patchfrog ops doctor`'s `github_webhook_secret` check -- a
   placeholder or mismatched secret rejects every delivery with 401
   before any processing happens.
4. Ingress/tunnel reachability from GitHub's IP ranges.

## Worker outage

Symptom: `patchfrog ops stale` lists runs stuck `RUNNING`, or `ops
health`/`doctor`'s `redis` check fails (broker unreachable means no
task can even be picked up). Restart the worker process; recover stuck
runs with `patchfrog ops stale --recover` afterward (marks them
`FAILED` locally, never touches GitHub), then `patchfrog ops retry
<review_run_id>` for ones that should re-run.

## Rollback a bad PatchFrog deployment

1. `alembic downgrade -1` only if the new migration is itself the
   problem -- confirm first via `patchfrog ops doctor`'s `database`
   check whether the issue is even migration-related.
2. Redeploy the previous image tag/commit for `api`/`worker`.
3. `GET /health/ready` and `patchfrog ops doctor` on the rolled-back
   deployment before considering it recovered.

No in-flight review/publication work is lost by a rollback -- Celery's
own retry/idempotency (see `docs/deployment.md`'s "Docker image notes"
on graceful shutdown, and `docs/production-e2e.md`'s "Retry /
idempotency") means a task interrupted mid-flight is either resumed or
safely re-runnable, never silently duplicated.

## Rotate provider key

1. Generate a new key in the provider's own console.
2. Update the secret in your deployment platform's secret manager (or
   `.env` for a bare-metal deployment) -- never in `.patchfrog.yml`,
   never committed.
3. Restart the worker (the only process that reads it).
4. `patchfrog ops doctor` to confirm the new key is present (length
   only, never the value).
5. Revoke the old key in the provider's console once you've confirmed a
   real review succeeds with the new one.

## Rotate webhook secret / GitHub App private key

1. In the GitHub App's settings: generate a new webhook secret, and/or
   generate a new private key (the old one keeps working until you
   revoke it -- no forced cutover window).
2. Update `GITHUB_WEBHOOK_SECRET` / `GITHUB_PRIVATE_KEY_PATH` (or
   `GITHUB_PRIVATE_KEY`) in your secret manager, never committed.
3. Restart the API (`GITHUB_WEBHOOK_SECRET`) and worker
   (`GITHUB_PRIVATE_KEY`/`_PATH`, since it's the one process making
   authenticated GitHub API calls).
4. `patchfrog ops doctor` to confirm both are present and well-formed.
5. Revoke the old private key in the GitHub App's settings once
   confirmed.

No command in this runbook ever prints a secret value -- every check
above reports presence/length/shape only.
