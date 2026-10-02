# M10 + M11 -- Phase 0 audit and implementation map

Baseline: `patchfrog` main `66cf5fa55ca8623a5214dd685406704636473f22`.
Cloud baseline: `patchfrog-cloud` main `2eb9a29` (pins engine `989567c`, pre-M4).

Invariant: `change -> impact -> evidence -> verification -> decision`.

This file is written before any code. It records what already exists, what
is reused, what is generalized, and where each new thing lives.

## 1. What exists in the public engine (reused, not rebuilt)

| Need | Existing primitive | Notes |
|---|---|---|
| Dependency identity / inventory | `patchfrog.dependencies` (`ExternalDependency`, `DependencyInventory`, `discover_dependencies`) | Provider-agnostic; env-var *names* only. |
| Persistent registry | `DependencyRegistry` + `external_dependencies`, `external_dependency_usage_sites`, `external_contract_snapshots` | Per repository, idempotent, has `last_observed_at`. This is the freshness source. |
| Upstream change | `patchfrog.upstream` (`ExternalChangeEvent`, `build_contract_change`, `build_version_change`, fingerprint) | Event fingerprint is already the dedup identity. |
| Multi-repo impact | `upstream.workspace` (`analyze_inventory`, `analyze_workspace`, `registry_impact`) | Already sorts repos into affected / uncertain / unaffected. Works from checkouts *or* registry rows. |
| Blast radius per repo | `upstream.blast_radius.compute_blast_radius` | Direct/transitive/potential. |
| Migration plan / patch | `migration.planner.plan_migration`, `migration.generator.generate_patch` | Operate on a local `root: Path`. |
| Verification | `migration_verification.service.run_migration_verification` -> `MigrationEvidenceBundle` | Outcome precedence fixed in PR #67 (`decision.py`). |
| PR decision + dossier | `migration_pr.planner.build_pr_plan`, `eligibility`, `dossier`, `integrity` | Pure; stale-base + integrity checks live in `MigrationPRPublisher`. |
| PR publication | `MigrationPRPublisher` over `MigrationGitHubPublisher` protocol; `GitHubClientMigrationPublisher`; `FakeGitHub` | The GitHub side effect is already behind a protocol. Cloud only has to supply credentials. |
| Snapshot acquisition | `RepositorySnapshotProvider.acquire(clone_url, commit_sha, token=...)` | Untrusted-tree safe; no code execution. |
| Installation tokens | `github.auth.InstallationTokenProvider` | Cloud already uses `build_app_jwt`. |
| Metrics | `ops.metrics` (low-cardinality Prometheus, multiprocess mode) | Extend, do not replace. |

## 2. What the older cross-repo work gives M10, and what it does not

`patchfrog.cross_repo_intelligence` + `repository_contract_keys` +
`repository_relations` is **PR-review-centric**: it answers "does an
explicitly linked peer consume a contract the *current PR* changed?" and only
feeds a review-hint. It stays as-is (CLAUDE.md layer R). What M10 reuses from
it is its **trust rule**: relationships are explicit, never inferred from
names/orgs/similarity, and never taken from PR-controlled files.

What must be *generalized* (not copied):

- the unit of analysis changes from "current PR" to "upstream change event";
- the repo set changes from `MAX_CROSS_REPO_PEERS = 4` neighbours to every
  enrolled repository in a workspace;
- the output changes from a review signal to a persistent campaign.

Decision: M10 does **not** write to `repository_relations`. Internal
contracts (M10.5) are modelled as an explicit `DependencyTarget` identity
that the existing `match_dependency` already knows how to match against
consumers' M5 evidence. That avoids a second relation graph.

## 3. Where state lives

| State | Home | Why |
|---|---|---|
| Org compatibility graph | **Derived view**, no new table | Built from M5 registry rows grouped by dependency identity. Storing it would duplicate per-repo truth. |
| `CompatibilityCampaign` + per-repo records | Public, new tables `compatibility_campaigns`, `campaign_repository_records` (Alembic `0038`) | Self-host relevant engine truth. Keyed by an opaque `workspace_key` string; the engine has no notion of a Cloud workspace. |
| Watcher snapshot normalization + diff | Public code, no table | Pure functions over a cursor/snapshot value. |
| Cursor / schedule / attempts / backoff | **Cloud** `cloud_upstream_sources` | Operating the service. |
| Workspace policy, quota, jobs, dashboard | **Cloud** | SaaS lifecycle. |

One Alembic head per repo is preserved: public `0037 -> 0038`; Cloud adds one
revision on its own chain.

## 4. Where continuous polling belongs

Public: *what* to watch (`watchers.registry` aggregates active dependency
identities into deduplicated watch requirements), *how* to fetch/normalize/
fingerprint/diff (`watchers.adapters.*`, `watchers.diff`).
Cloud: *when* (Celery beat tick, due-source selection, jitter, retry/backoff,
per-host rate limiting, cursor persistence, failure visibility).

## 5. Public vs private, per component

| Component | Repo |
|---|---|
| Campaign domain, state machines, org blast radius, freshness, dossier, internal-contract identity, orchestration, CLI/demo | public |
| Watcher interface, source adapters, normalization, diff, watch-requirement aggregation | public |
| Beat schedule, source rows/cursors, due selection, retries, rate limits | Cloud |
| Workspace watch/verification/publication policy | Cloud (enforced by a *public* pure gate function so the semantics are inspectable) |
| Installation tokens, GitHub App credentials, executing the PR side effect | Cloud |
| Dashboard, usage counters, notifications/outbox | Cloud |
| Fake GitHub, fake fetcher, E2E fixtures | public (Cloud tests import them via the pinned dependency) |

## 6. Obsolete / redundant after the pivot (noted, not deleted here)

- The generic PR-review pipeline remains a secondary safety layer; untouched.
- Cloud's dashboard center of gravity (review history) is demoted, not removed.
- Cloud pins engine `989567c`; M4-M9 are not reachable until the pin moves.
  Bumping it is a deliberate, explained change in the Cloud PR.

## 7. Hard constraints discovered during the audit

1. M7/M8 work on a **local checkout**. In Cloud, a worker must acquire a
   snapshot per repo (installation token -> `RepositorySnapshotProvider`)
   before analysis. Tokens must never enter a job payload.
2. M8 needs a bwrap sandbox. Cloud workers without it can only reach
   `UNVERIFIED`/`HUMAN_REQUIRED`; this must be reported, never papered over.
3. Cloud and the engine share one Postgres with separate Alembic chains;
   Cloud tables reference engine rows only by GitHub ids.
4. Both repos' CI currently expand a disposable private key through workflow
   `env`, which prints it in job logs. Fixing this is mandatory (M11.13).
5. Production Postgres volume was filled and production is frozen. This task
   makes the code/deploy reproducible and documents a restore plan; it does
   not touch production.

## 8. Planned public package layout

```
patchfrog/campaigns/   domain, freshness, graph, blast, internal, state,
                       evaluate, orchestrate, dossier, store, report
patchfrog/watchers/    domain, fetch, adapters/{pypi,npm,github_releases,
                       openapi_url,changelog_feed,manual}, diff, registry,
                       gate (policy), pipeline
```

## 9. Version constants

New: `CAMPAIGN_ENGINE_VERSION = 1`, `WATCHER_ENGINE_VERSION = 1`.
Not bumped: review/prompt/telemetry/quality-cost versions (no review-path
change). `UPSTREAM_CHANGE_VERSION`, `MIGRATION_PR_VERSION`, M8 versions
unchanged -- M10/M11 compose them without changing their semantics.

## 10. False-positive / cost discipline

No candidate, no provider call. Every M10/M11 step is deterministic; the
campaign/watcher packages must never import a model provider (structural test,
same pattern as `test_*_never_imports_a_provider`). A watcher change that does
not reach a consumer produces `NOT_AFFECTED`, never a finding.
