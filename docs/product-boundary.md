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
