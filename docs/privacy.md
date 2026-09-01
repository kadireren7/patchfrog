# Privacy and Data Handling

A factual summary of what PatchFrog's code actually does with a
repository's source and a user's activity -- not a legal policy. For
self-hosted PatchFrog, *you* (the operator) control where this data
lives; this page describes what the application itself does with it,
not any hosting arrangement.

## Source code

- A pull request's diff and the surrounding repository content PatchFrog's
  Context Engine selects (see `docs/context-engine.md`) are sent to
  whichever AI provider you configured (`ANTHROPIC_API_KEY` /
  `GEMINI_API_KEY`) as part of the review prompt -- that provider's own
  data policy governs what happens to it on their side. See
  `docs/deployment.md`'s "Data policy" note specifically for Gemini's
  free tier (prompts/responses may be used to improve Google's products
  -- treat free-tier Gemini as suitable only for public/non-confidential
  code until you're on a paid tier).
- Raw source file content, full diff text, raw prompts, and raw provider
  responses are **never persisted** by PatchFrog -- only the structured,
  validated finding fields (title, message, category, severity,
  confidence, file path, line range, a short evidence snippet) are
  written to `ai_findings`/`ai_finding_proposals`. See the module
  docstring of `patchfrog.persistence.models.review`.
- Temporary local checkouts (`patchfrog-snapshot-*`) are deleted on
  every exit path (success, failure, or exception) -- see
  `docs/operations.md`'s "Data retention".

## What's persisted, and for how long

During beta, database rows are retained indefinitely: `ai_findings`,
`feedback_events` (append-only, tombstone-preserving on reaction
removal), review run metadata, and publication records. This is an
operational choice for an early beta (auditability), not a permanent
retention policy -- see `docs/operations.md`'s "Data retention" for the
exact table.

## Telemetry (`patchfrog.telemetry`)

Structured metadata only, reconstructed from already-persisted state --
never a second copy of source code. Every field is an id, a count, an
enum, a token/latency number, or at most a file path + line range.
**Never persisted in telemetry**: raw source content, full diff text,
raw prompts, raw context snippets, quoted evidence text, or provider API
response bodies. See `docs/telemetry-intelligence.md`'s own privacy
section and its `*_no_secret*`/`*redaction*` test coverage for the
enforced guarantee, not just a stated intent.

## Feedback (`patchfrog.feedback`)

Only a reacting/replying actor's **GitHub login and whether it is a
bot** are ever persisted -- no profile, no cross-repository
correlation, no reputation score. Reply body text is **never
persisted** at all (only that a reply occurred, and its normalized
signal). `patchfrog feedback export` never includes actor logins or
raw evidence text by default -- only a hash of the finding's evidence.
Feedback is poll-only (`patchfrog feedback sync`), never webhook-driven
-- see `docs/feedback.md`'s own "Privacy" section.

## GitHub metadata

Repository full name, installation id, pull request number, commit
SHAs, and finding file paths/line ranges are persisted as the minimum
needed to correlate a review to the PR it's about and to publish/
retrieve comments. No GitHub user profile data beyond what's described
above (login, bot flag) is ever stored.

## Credentials

`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `GITHUB_WEBHOOK_SECRET`, and the
GitHub App private key are environment-only (or an injected secret
manager value) -- never read from `.patchfrog.yml`, never logged, never
persisted in any database table, and never present in a telemetry
export. Structured logs run through a defense-in-depth redaction
processor (`patchfrog.config.logging.redact_secrets`) that strips
field names shaped like `*token*`/`*secret*`/`*key*`/`authorization`/
`password` and any PEM block or `Bearer ...` token appearing anywhere in
a log line, including inside a formatted traceback -- see
`docs/operations.md`'s "Structured logging".

## What this document does not claim

- **No claim of zero data retention by third-party AI providers.**
  Anthropic's and Google's own data policies govern what happens to a
  prompt once it's sent to them -- PatchFrog has no visibility or
  control over that beyond choosing which provider/model to use.
- **No claim of encryption-at-rest or any compliance certification**
  (SOC 2, HIPAA, etc.) -- none is implemented. Self-hosted PatchFrog's
  actual data protections are whatever your own PostgreSQL/Redis/
  infrastructure deployment provides; this document describes
  PatchFrog's own application-level behavior only.
- **This is not a security certification of any kind** -- see
  `docs/external-beta.md`'s "Beta limitations".

## Self-hosted means you control the infrastructure

Every claim above is about what PatchFrog's *code* does. Since you run
the database, Redis, API, and worker yourself, you also control
physical/cloud data residency, backup policy, and access control for
all of it -- none of that is a PatchFrog decision to make on your
behalf.
