# Onboarding a repository

## The flow

1. A user installs the PatchFrog GitHub App and selects repositories
   (all, or a subset).
2. GitHub sends `installation` (`created`) and `installation_repositories`
   (`added`) webhook events. PatchFrog persists an `installations` row
   and marks the selected repositories `is_selected=True` -- **no manual
   database edit is ever required.**
3. The user opens (or updates) a pull request. GitHub sends a
   `pull_request` event.
4. PatchFrog checks eligibility (installation active, beta state active,
   repository selected, quota not exceeded, kill switches clear -- see
   `patchfrog.ops.eligibility`) and, if eligible, automatically runs
   index → analyze → review, with no further action from the user.
5. If publication is enabled for the installation (see below) and the
   repository's own `.patchfrog.yml` allows it, PatchFrog posts a real
   GitHub review comment.

Steps 2-5 all happen automatically, driven entirely by real GitHub
webhook events -- `patchfrog.ops.orchestrator` is what sequences the
existing index/analyze/review/publish stages together (see
`docs/operations.md` for the full pipeline).

## Branch scope: any branch, PR-driven only

`patchfrog.github.webhooks.parse_pull_request_event` treats `base_branch`/
`head_branch` purely as metadata carried through to persistence and
publication -- neither is ever checked against an allowlist, and nothing
downstream (`patchfrog.ops.eligibility`, `patchfrog.ops.orchestrator`)
filters on branch name. The only three `pull_request` webhook actions
PatchFrog acts on are `opened`, `reopened`, and `synchronize`
(`PullRequestEventAction`); every other action is ignored.

**Supported** (all reach scheduling once the eligibility checks above pass):

- `feature/foo` -> `main`
- `feature/foo` -> `develop`
- `hotfix/bar` -> `release/1.x`
- a fork branch -> a target repo PR, to the extent GitHub's own API
  permits it for the App's installation (see the fork-PR note below)
- a subsequent commit pushed to an already-open PR (`synchronize` ->
  incremental review, see `docs/evaluation.md`'s Phase 7 notes)
- a `reopened` PR
- an unusual-but-valid branch name (e.g. `feat/foo-bar_123`)

**Not supported, by design:**

- a branch pushed with no open pull request -- PatchFrog never
  subscribes to or processes a generic `push` event. It is a pull-request
  reviewer, not a push-triggered branch scanner, and adding a push-event
  path is a deliberate non-goal, not a gap.

**Fork-originated PRs**: the installation token scopes to the *base*
repository (`event.repository.owner/name`), never the fork, for every
GitHub API call (`GET /repos/{base}/pulls/{n}`,
`.../pulls/{n}/files`) and for the git fetch PatchFrog's indexer performs
(`patchfrog.repository.snapshot`, `clone_url` always constructed from the
base repository's `full_name`). This works because GitHub always creates
a `refs/pull/<n>/head` ref in the *base* repository for every PR
(fork-originated or not), and github.com's git protocol allows fetching
any commit reachable from any ref in a repository the token can read --
not only advertised branch refs. If a remote ever doesn't support
fetching an arbitrary SHA directly, `_fetch_one` falls back to a full
`fetch origin` and then fails the snapshot cleanly (a visible `GitError`,
never a silent partial checkout) rather than indexing incomplete/wrong
content. PatchFrog never clones or accesses the fork repository itself,
and never executes anything from the checked-out tree either way (see
`patchfrog.repository.git`).

## Publication is off by default -- two independent gates

A repository never gets real GitHub comments just by installing the
App. **Both** of the following must be true:

1. **Beta gate** (`InstallationModel.publication_allowed`, default
   `False`): an operator explicitly opts an installation in via
   `patchfrog ops installations --installation <id> --allow-publication`.
2. **Repository gate** (`.patchfrog.yml`'s `publish.enabled`, default
   `False`, unchanged from Phase 6): the repository owner opts their own
   repository in.

Neither alone is sufficient -- this is deliberate defense in depth so a
developer/test environment (where `publication_allowed` defaults `False`
process-wide via `GLOBAL_PUBLICATION_ENABLED`, see `docs/deployment.md`)
can never accidentally start writing real GitHub comments, and so a beta
operator retains a global override independent of what any individual
repository's config says.

## Beta allowlist mode

By default (`BETA_ALLOWLIST_MODE=false`), a new installation is
self-serve: `beta_state` starts `active` immediately on the `installation`
`created` event, and review processing (not publication -- see above)
starts working right away.

Set `BETA_ALLOWLIST_MODE=true` to require explicit activation: new
installations start `beta_state=pending` (no processing at all) until an
operator runs:

```
patchfrog ops installations --activate <github_installation_id>
```

## Minimal `.patchfrog.yml`

Entirely optional. No file at all means conservative defaults (no
publication, medium+ severity threshold, standard candidate/token
budgets -- see `patchfrog.review.config.ReviewConfig` and
`patchfrog.publishing.config.PublicationConfig`). A malformed file never
silently degrades a real review run: `patchfrog.review.config`'s
`on_malformed="raise"` path turns it into a visible, queryable failed run
rather than proceeding on defaults.

```yaml
publish:
  enabled: true
  min_severity: medium
```

That's the entire file most repositories will ever need. See
`patchfrog/review/config.py` and `patchfrog/publishing/config.py` for
every other (all optional) knob.

Selecting a non-default AI provider (Anthropic remains the default) is
**not** a `.patchfrog.yml` concern -- it's an operator/deployment
decision, made via environment variables the operator running this
PatchFrog instance controls, not by any repository being reviewed:

```
PATCHFROG_REVIEW_PROVIDER=gemini
PATCHFROG_REVIEW_MODEL=gemini-3.6-flash
```

See [Provider/model selection](deployment.md#providermodel-selection-operator-controlled)
for the required `GEMINI_API_KEY`, the provider-appropriate effective
defaults this normalizes to (`PATCHFROG_REVIEW_CRITIC_MODEL`,
`PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS`) and how to override them,
and the current free-tier data-policy/quota caveats.

## Repository/installation lifecycle

- `installation_repositories` `removed` -> `RepositoryModel.is_selected`
  flips `False` -> no new review work is ever scheduled for that
  repository again (checked in `patchfrog.ops.eligibility`, every time,
  before any pipeline stage runs).
- `installation` `suspend`/`deleted` -> `InstallationModel.status`
  updates -> every repository under that installation stops being
  eligible immediately, without needing to touch each repository row
  individually.
- A PR belonging to a repository whose recorded `installation_id`
  doesn't match the webhook event's installation id is never processed
  (`REPOSITORY_INSTALLATION_MISMATCH` -- fail closed on any ownership
  inconsistency, never guessed).

## What still requires a real permission (none, today)

Every event PatchFrog reacts to during onboarding
(`installation`/`installation_repositories`/`pull_request`) and every
GitHub API call during processing works with the App's existing
`contents:read`/`metadata:read`/`pull_requests:write` grant. No new
permission was requested for public-beta readiness.
