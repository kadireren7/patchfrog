# Product boundary: Self-Hosted / Source Available vs. PatchFrog Cloud

This document is architecture/product documentation only. **None of PatchFrog
Cloud's features described here are implemented in this repository as of
this document.** This repository is the self-hosted / source-available
codebase; see [`docs/licensing.md`](licensing.md) for the license that
governs it and [`TRADEMARK.md`](../TRADEMARK.md) for brand/identity rules
referenced below.

## PatchFrog Source Available / Self-Hosted

This is what this repository *is* -- the code you're reading. It contains,
or is expected to contain over time:

- Repository ingestion (GitHub webhook handling, PR metadata/diff fetch)
- Static Analysis Engine (`patchfrog/analysis/`)
- Context Engine (`patchfrog/context/`)
- AI reviewer and provider adapters (`patchfrog/review/`,
  `patchfrog/review/providers/`)
- Critic/verifier (`patchfrog/review/critic.py`)
- Incremental review memory (`patchfrog/review_memory/`)
- Publishing engine (`patchfrog/publishing/`)
- Feedback engine (`patchfrog/feedback/`)
- Evaluation harness (`patchfrog/evaluation/`)
- Self-hosted GitHub App integration -- **you** create and control the
  GitHub App used against your own repositories (see
  [GitHub App boundary](#github-app-boundary) below)

Self-hosting means running all of the above yourself: your own Postgres/
Redis, your own worker process, your own GitHub App, your own AI provider
credentials. See [`docs/deployment.md`](deployment.md) for how, and
[`docs/onboarding.md`](onboarding.md) for the app-installation flow once
it's running.

## PatchFrog Cloud

PatchFrog Cloud is the official hosted product. **It is planned / under
development, not yet generally available**, and nothing described in this
section exists in this repository. When it exists, it is expected to
additionally include things that don't belong in a self-hosted codebase at
all:

- Hosted deployment (PatchFrog operates the infrastructure)
- The official `patchfrog[bot]` bot identity (see
  [GitHub App boundary](#github-app-boundary))
- Account / organization management
- Managed provider/model routing (see [Cloud model](#patchfrog-cloud-model)
  below)
- Multi-tenant infrastructure
- Usage accounting
- Plans and billing
- Hosted analytics / a dashboard
- Managed data retention and privacy controls
- Enterprise controls (SSO, audit logs, org-level policy, etc.)

None of this is being built as part of this PR, and none of it is being
implied to already exist.

## GitHub App boundary

Two GitHub App identities exist in this model, and they are never shared:

**Official PatchFrog Cloud**

- Uses the official PatchFrog GitHub App.
- Official bot identity: `patchfrog[bot]`.
- Official App credentials (App ID, private key, webhook secret) remain
  private and operator-controlled -- never distributed, never checked into
  any repository, including this one.
- Users who install the official App connect to PatchFrog Cloud.

**Self-hosted PatchFrog**

- You create your own GitHub App in your own GitHub account/organization.
- You configure your own App ID, private key, and webhook secret (see
  [`docs/deployment.md`](deployment.md)'s "Required runtime secrets").
- You use your own AI provider API key(s) (see [BYOK](#byok-bring-your-own-key-self-hosted)
  below).
- Your bot's GitHub identity is **your own App's identity**, not
  `patchfrog[bot]` -- see [`TRADEMARK.md`](../TRADEMARK.md) for why this
  matters.
- Self-hosting **never** grants you access to PatchFrog Cloud's official
  App credentials, infrastructure, or account data. The two are completely
  independent deployments that happen to run the same source-available codebase.

No real App secret, key, or credential is (or should ever be) placed in
this documentation. Every example in `docs/deployment.md` uses placeholder
values only.

## BYOK (bring your own key), self-hosted

Self-hosted PatchFrog is a BYOK (bring-your-own-key) model for AI provider
access:

- Supported examples today: `GEMINI_API_KEY`, `ANTHROPIC_API_KEY` (see
  `patchfrog/config/settings.py`). Future providers will follow the same
  pattern.
- Provider credentials are **operator-controlled runtime secrets** --
  environment variables / a secret manager, exactly as documented in
  [`docs/deployment.md`](deployment.md#required-runtime-secrets). They are
  never read from `.patchfrog.yml` or any other repository-controlled file
  (see `patchfrog.review.config.load_review_config`'s explicit rejection of
  credential-shaped fields there).
- Credentials are never committed to this repository, and **your**
  repositories (the ones PatchFrog reviews) should never contain provider
  API keys either -- PatchFrog never reads secrets out of the code it's
  reviewing.
- Which provider/model a self-hosted instance uses is chosen via
  operator/deployment environment variables (`PATCHFROG_REVIEW_PROVIDER`,
  `PATCHFROG_REVIEW_MODEL`, and related -- see
  [`docs/deployment.md`](deployment.md#providermodel-selection-operator-controlled)),
  never via `.patchfrog.yml`. This is a deliberate trust/cost boundary:
  a reviewed repository cannot choose PatchFrog's provider/model, force
  a more expensive model, or swap the critic model -- only the operator
  running the deployment can. `.patchfrog.yml` still controls review
  *behavior* (candidate/token budgets, confidence thresholds, and so
  on), just never provider/model identity.

## PatchFrog Cloud model

The intended (not yet built) PatchFrog Cloud user experience:

1. User signs in to PatchFrog Cloud.
2. User installs the **official** PatchFrog GitHub App.
3. User selects which repositories PatchFrog Cloud may access.
4. User opens pull requests as normal.
5. `patchfrog[bot]` reviews automatically -- no self-hosted infrastructure,
   no provider key of the user's own.

Key differences from self-hosted, by design:

- Cloud users do **not** provide their own Gemini/Anthropic (or other
  provider) keys by default -- PatchFrog Cloud manages the provider
  credential.
- Cloud users do **not** choose a raw provider/model name themselves.
  PatchFrog Cloud manages provider selection and routing internally, and
  that internal routing may change over time (e.g. a model upgrade)
  **without requiring any change to a user's repository configuration**.
- This is architecture/product documentation only -- no routing,
  multi-tenant, billing, or account-management code is added by this PR.

## Repository split and the engine/Cloud relationship

The governing rule, restated precisely: **if it determines how PatchFrog
reviews code, it belongs in this source-available engine; if it operates
PatchFrog as a hosted SaaS business, it belongs in the private Cloud control
plane.**

- **Public**: `kadireren7/patchfrog` (this repository).
- **Private**: `kadireren7/patchfrog-cloud` -- created; Milestone W (Cloud
  Foundation) and X (Private Beta) are under development there. Cloud
  implementation must never be added under `patchfrog/cloud/` in this
  repository -- it lives exclusively in the private repository.
- **Cloud consumes the engine; it never forks or duplicates it.** Cloud
  depends on this engine as a pinned library dependency (a tagged/pinned
  `main` commit of `kadireren7/patchfrog`), calling the engine's existing
  services (webhook signature verification, installation sync, PR
  ingestion, review pipeline, Merge Readiness) directly rather than
  reimplementing a second review pipeline, a second Intelligence layer, or
  a second Quality + Cost Guard. Engine decisions (how a PR is reviewed)
  stay owned by this repository even now that Cloud exists. Cloud's own
  tables (accounts, workspaces, installation ownership, usage/quota) are
  additive: they reference the engine's own GitHub-identifier space
  (`github_installation_id`, `github_repository_id`) rather than
  duplicating the engine's own installation/repository/review-run rows.

The following newer engine components extend this same split:

- **Generic model router** (Milestone U, shipped): the routing *algorithm*
  (how to pick a provider/model given a declared policy) lives in this
  engine (`patchfrog.routing`). Cloud's own *production* routing
  configuration -- provider weights, rollout percentages, experiment
  assignment, provider-health state, Cloud fallback policy -- is Cloud-only
  and never enters this repository, even as sample configuration.
- **Executable Verification engine** (Milestone S, extended by S6,
  shipped): the verification domain, eligibility rules, adapters, sandbox
  interface, deterministic execution policy, result classification, the
  separate credential-minimal verifier process (`apps/verifier/`), and its
  request/result protocol (`patchfrog.executable_verification.protocol`)
  are all source-available. Cloud owns the *production* sandbox fleet:
  verifier autoscaling, container/job orchestration, isolation
  infrastructure configuration, Cloud quotas, hosted caching, abuse
  prevention, and Cloud execution billing -- operational concerns around
  running the engine at hosted scale, never a second verification engine or
  a second verifier protocol.
- **Agent Handoff / MCP protocol layer** (Milestone T, shipped): the
  protocol layer and the shape of evidence handed to a coding agent are
  source-available. Any Cloud-hosted agent marketplace, billing for agent
  usage, or Cloud-specific agent orchestration policy would be Cloud-only,
  if/when it exists.
- **Merge Readiness / Decision Layer** (Milestone V, shipped): the
  `READY` / `BLOCKED` / `HUMAN_REVIEW_REQUIRED` decision semantics and the
  read-only `get_merge_readiness` computation live only in this engine
  (`patchfrog.merge_readiness`). Cloud's dashboard surfaces this exact
  result for the exact PR head SHA -- it never recomputes readiness, adds a
  numeric score, or diverges from engine semantics.
- **Accounts, billing, organizations/workspaces, admin tooling, hosted
  infrastructure**: always private Cloud-only, per the "PatchFrog Cloud"
  section above -- never duplicated or stubbed out in this repository. The
  official `patchfrog[bot]` GitHub App identity, hosted GitHub App
  credentials, Cloud auth/session handling, and Cloud production provider
  routing policy live only in `patchfrog-cloud`.
- **Repository & Organization Learning** (Milestone Y): the durable
  learning-derivation logic (`patchfrog.learning_records`) -- what
  counts as repeated/independent evidence, maturity classification,
  personalization effects -- is source-available. The public engine has
  no workspace concept and never aggregates across tenants on its own;
  organization-level aggregation is a generic primitive that only ever
  acts on an explicit `repository_ids` scope Cloud supplies. Cloud owns
  workspace-to-repository scoping, the "Learnings" dashboard, and any
  per-workspace/per-repository learning controls (disable
  personalization, retire a learning manually).
- **Policy & Governance** (Milestone Z): the deterministic policy
  evaluation engine, precedence-merge semantics, and integration with
  Merge Readiness/the Model Router/Executable Verification
  (`patchfrog.governance`) are source-available. Cloud owns policy
  *definition* (`PolicyDefinitionModel`), *assignment*
  (workspace-wide + repository override, override may only tighten),
  the audit log, and the policy UI -- never a second policy evaluation
  engine.

See `docs/roadmap.md` for how these milestones are sequenced and why.
