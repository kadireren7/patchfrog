# Compatibility campaigns (M10)

One upstream API/SDK change usually reaches several repositories of one organization. A
**compatibility campaign** is the single, durable, honest record of that fact: which repositories were
in scope, how each was classified, how far each migration got, and what is still unresolved. It is
built entirely from the M5 → M9 machinery (dependency registry, upstream change, blast radius,
migration plan/patch, verification, PR publication); it adds no new analysis and no model call.

```
upstream change ──► every enrolled repository evaluated independently
                        (fresh? readable? affected? migration? verified? PR?)
                ──► one CompatibilityCampaign  (idempotent per workspace + change + engine version)
```

Package: `patchfrog/campaigns/`. Version constant: `CAMPAIGN_ENGINE_VERSION = 1`.

## The organization graph (M10.1)

`campaigns.graph.build_org_graph` derives, on demand, `workspace → repositories → dependency identities →
per-repository instances (version, usage sites, contract)`. It is a **view**, never stored: a dependency
identity is shared logically across repositories (an SDK consumer, a direct-REST consumer and a
generated-client consumer all land under one provider identity) while every per-repository fact stays owned
by that repository's own M5 rows.

## Repository and campaign states (M10.3)

Repository states: `NOT_AFFECTED`, `IMPACTED`, `MIGRATION_PLANNED`, `PATCH_GENERATED`, `VERIFYING`,
`VERIFIED`, `PARTIALLY_VERIFIED`, `HUMAN_REQUIRED`, `FAILED`, `PR_OPENED`, `RESOLVED`, plus the
uncertainty states that exist so doubt is representable instead of rounded away: `STALE`, `UNKNOWN`,
`ACCESS_LOST`, `UNVERIFIED`.

Campaign states: `DETECTED`, `ANALYZING`, `ACTION_REQUIRED`, `MIGRATING`, `PARTIALLY_RESOLVED`,
`RESOLVED`, `BLOCKED`. Precedence (`campaigns/state.py`, first match wins):

1. nothing evaluated → `DETECTED`; some unevaluated → `ANALYZING`
2. every repository `NOT_AFFECTED`/`RESOLVED` → `RESOLVED` (the **only** road to it)
3. at least one `RESOLVED` and anything unfinished → `PARTIALLY_RESOLVED`
4. blocking repositories, all system-blocked (`FAILED`/`ACCESS_LOST`), nothing in flight → `BLOCKED`
5. blocking repositories (`IMPACTED`, `HUMAN_REQUIRED`, `FAILED`, `PARTIALLY_VERIFIED`, `UNVERIFIED`,
   `STALE`, `UNKNOWN`, `ACCESS_LOST`) → `ACTION_REQUIRED`
6. otherwise → `MIGRATING`

`RESOLVED` is reached only by **fresh re-evaluation** showing a previously affected repository is no
longer affected — never by "a PR was opened" or "a patch exists".

## Honesty rules

* **Freshness (M10.7).** Each repository's evidence is `FRESH`, `STALE` (older than the window,
  default 7 days, configurable), `UNKNOWN` (never discovered, or a clock/record fault) or `ACCESS_LOST`.
  A repository whose evidence is not fresh is **not analyzed at all** and is never reported unaffected.
* **Uncertainty is never "safe".** `OrgBlastRadius.safe` is true only if every enrolled repository is
  positively known unaffected. Version-level-only evidence is `UNKNOWN`, not `AFFECTED`.
* **Failure is isolated.** Any exception evaluating or publishing one repository becomes that
  repository's `FAILED` record (credentials scrubbed) and cannot touch another.
* **Evidence that could not be obtained is not weaker evidence.** A caller that tried and failed to
  check a repository out passes `evidence_error`; the repository is `FAILED`, never classified from
  registry data that, for an SDK without a built-in discovery adapter, would read as "not affected".
* **Scope is explicit (M10.6).** Only repositories passed in are evaluated — there is no implicit
  access. A repository no longer enrolled leaves the campaign; one enrolled but unreadable stays as
  `ACCESS_LOST`.

## Orchestration (M10.4) and publication

`campaigns.orchestrate.run_campaign` evaluates every repository (M6 impact → M7 plan/patch → M8
verification → M9 PR plan), then publishes **per repository** under the workspace policy
(`campaigns/policy.py`: watch mode `off | detect_only | migrate | migrate_and_open_pr`, verification
requirement `require_verified | allow_partially_verified`, publication `automatic | draft_only |
manual_approval`). Safe defaults: detect and report, require verified, manual approval. A partially
verified PR is always a draft. A human approval (`approved_repositories`) turns `AWAIT_APPROVAL` into a
publication only after a **fresh** evaluation, and never lowers the bar. Each repository keeps its own base
SHA, patch fingerprint, evidence bundle, PR and publication state. Nothing ever merges.

## Internal contracts (M10.5)

An organization's own contract (a published package, an OpenAPI spec, a generated client, an internal SDK)
is an `InternalContract` with an **explicit** identity: package (ecosystem + name), declared API hosts, or an
M5 dependency key. No relationship is inferred from repository names, organization, layout or similarity.
Consumers are matched by their *own* M5 evidence through the same `match_dependency` external changes use;
the producer repository is the origin, not a consumer. There is no second graph and no new relation table.

## Idempotency (M10.9) and persistence

`campaign_identity_key(workspace_key, change_fingerprint, engine_version)` is the campaign's identity;
re-running with unchanged inputs yields an equal campaign (same `version`). Repository-level operations
stay separately idempotent (M9's PR identity is repository + change + engine version). Tables
`compatibility_campaigns` and `campaign_repository_records` (Alembic `0038`); `workspace_key` is an opaque
string the caller chooses (a hosted workspace id, an installation id, an org login).

## Dossier (M10.8)

`campaigns.dossier.campaign_to_dict` / `render_campaign_markdown`: upstream change, blast-radius counts,
per-repository state/freshness/impact/verification/PR, residual risk, human-required actions, a single
next action per repository, and per-repository data freshness — derivable from persisted state alone.

## CLI

```bash
patchfrog campaigns demo           # the bundled four-repository scenario, offline, deterministic
patchfrog campaigns analyze --old OLD --new NEW --hints H \
    --repo web=./web --repo legacy=./legacy --stale legacy --policy migrate
```

The demo (`patchfrog/campaigns/demo.py`, fixtures in `tests/fixtures/campaigns/acme`): a watcher baselines
then detects `client.chat.create → client.responses.create`; one campaign covers four repositories — one
auto-fixable and verified (one PR), one human-required, one not affected, one with stale discovery — and is
`ACTION_REQUIRED`, not resolved; an identical re-run creates no second campaign or PR; after the PR is
merged, the human fix is made and the stale repository is rediscovered, it reconciles to `RESOLVED`.

## Known limitations

* Verification needs a working `bwrap` sandbox; without it nothing is ever `VERIFIED`
  (`docs/executable-verification.md`).
* An SDK with no built-in discovery adapter is recorded in the registry by package declaration only
  (no call sites). Consumers are therefore found by **live analysis of a checkout** (using the change's own
  target adapters), not by the registry alone — see `docs/watchers.md`.
* Transitive blast radius follows the in-repository code graph only; cross-repository transitivity is not
  inferred.
