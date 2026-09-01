# External Beta

What an invited beta user is actually getting when they run PatchFrog
today. Read this before inviting anyone, and hand it to the people you
invite.

## What you get

**PatchFrog Self-Hosted / Source-Available** -- the only thing that
exists right now:

- *You* run PatchFrog: API + worker processes, PostgreSQL, Redis --
  see `docs/quickstart.md` and `docs/deployment.md`.
- *You* own the GitHub App -- created under your own GitHub account/org,
  installed on your own repositories.
- *You* provide the AI provider credentials (`ANTHROPIC_API_KEY` and/or
  `GEMINI_API_KEY`) -- PatchFrog never ships or proxies a credential of
  its own. Provider usage costs are billed to *your* provider account,
  not PatchFrog's.
- *You* control repository eligibility and publication -- three
  independent, operator-controlled gates, all off by default (see
  "Publish disabled" below). Nothing self-enrolls.
- PatchFrog's source is [Elastic License 2.0](../LICENSE)
  (source-available), not open-source -- see `docs/licensing.md` for
  exactly what that means in practice.

## What you don't get (yet)

**PatchFrog Cloud** is planned / under development. It does not exist
today. Specifically, none of the following exist:

- A hosted PatchFrog you don't have to run yourself.
- A shared or "official" `patchfrog[bot]` App you can just install.
- Any billing relationship with PatchFrog for AI review usage.
- A web dashboard.

If any of the above would be useful to you, that's expected feedback for
a future milestone -- it is out of scope for this beta.

## Beta limitations

Be explicit about these with anyone you invite:

- **Support is limited.** This is a beta running on one operator's own
  infrastructure and time, not a supported product with an SLA.
- **Review output may be imperfect.** PatchFrog is an AI-assisted
  reviewer, not a certified static analysis tool or a security audit --
  see `docs/quality-cost-guard.md` and `docs/evaluation.md` for how
  quality is actually measured (precision/recall against a benchmark
  corpus, never a marketing claim). A human reviewer remains the
  authoritative approval for any pull request; PatchFrog's own comments
  say so implicitly by never claiming certainty it doesn't have (see
  `docs/brand.md`).
- **Provider/model quality and availability vary.** They are Anthropic's
  and Google's respectively, not PatchFrog's, and can change without
  PatchFrog's involvement (a model gets deprecated, a rate limit
  tightens, a data policy changes -- see `docs/deployment.md`'s "Data
  policy" note on Gemini's free tier specifically).
- **This is not a security certification of any kind.** Publishing
  PatchFrog's findings, or having none published, says nothing about a
  repository's actual security posture.
- **No guaranteed SLA, uptime, or response time** for this beta.

## Recommended initial rollout

- 3-5 repositories total, one at a time initially rather than all
  installed simultaneously.
- Prefer small-to-medium pull requests over very large ones while
  validating the flow (existing hard limits -- `MAX_CHANGED_FILES`,
  `MAX_DIFF_BYTES`, see `docs/deployment.md` -- still apply regardless).
- Enable publication only after `patchfrog ops preflight --repository
  owner/repo` reports `PUBLISH`, not before -- see `docs/beta-runbook.md`.
- Keep operator hard caps conservative (`PATCHFROG_MAX_REVIEW_*`, see
  `docs/deployment.md`'s Quality + Cost Guard section) until you've
  observed real cost/latency for your own repositories.
- Gemini is the more recently, more extensively live-validated provider
  as of this milestone (see `validation/production_e2e/`) -- a
  reasonable default recommendation for a first beta repository, not a
  requirement; Anthropic remains fully supported and is PatchFrog's
  configured default.
- Consider `BETA_ALLOWLIST_MODE=true` (see `docs/onboarding.md`) so a
  new installation starts `pending` and requires your explicit
  `patchfrog ops installations --activate` before any review processing
  begins at all -- the natural setting for a small, invite-only beta,
  as opposed to the self-serve default meant for a wide-open beta.

These are runbook guidance, not permanent product restrictions --
nothing here is enforced in code beyond the existing operator caps
themselves.

## Where to go next

- `docs/quickstart.md` -- get a self-hosted instance running end to end.
- `docs/beta-runbook.md` -- day-to-day operator playbook once you're
  running.
- `docs/beta-invite-checklist.md` -- the one-page checklist for each
  repository you invite.
- `docs/privacy.md` -- exactly what PatchFrog does and does not do with
  a repository's source and a user's feedback.
