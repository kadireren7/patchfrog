# Security Policy

## Supported versions

PatchFrog does not yet have a tagged release train -- `main` is the
only supported version. Run `patchfrog ops doctor` (its
`deployed_commit` line) or `git rev-parse HEAD` to identify exactly
which commit a given deployment is running.

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability or for
anything that includes secrets.**

GitHub's private vulnerability reporting feature is not yet enabled on
this repository. Until it is:

1. Open a GitHub issue with a **minimal, non-sensitive** title and body
   (e.g. "requesting a private security contact channel") -- do **not**
   include exploit details, proof-of-concept code, or any secret in
   that issue.
2. A maintainer will follow up to establish a private channel for the
   actual report.

If private vulnerability reporting has since been enabled for this
repository (check the repository's Security tab), prefer that instead
of the issue-based fallback above.

## What counts as in scope

- The PatchFrog application itself (`patchfrog/`, `apps/`) -- webhook
  signature verification, GitHub App authentication, publication safety
  gates, secret handling, and the AI review pipeline's trust boundaries
  (see `docs/deployment.md`'s "Provider/model selection" and
  `docs/operations.md`'s "Known security-boundary limitations" for
  what's already documented as a known, judged-low-risk gap).
- Deployment configuration guidance in `docs/` that, if followed,
  would result in an insecure deployment.

## What's out of scope

- Findings that require an attacker to already control a repository
  PatchFrog is configured to review (see `docs/operations.md`'s "Known
  security-boundary limitations" -- the installing party already
  controls their own repository content).
- Vulnerabilities in third-party dependencies -- report those upstream;
  a PR bumping a vulnerable pinned dependency here is still welcome.
- Findings against PatchFrog Cloud -- it does not exist yet (see
  `docs/external-beta.md`).
