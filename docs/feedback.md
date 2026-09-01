# PatchFrog feedback loop (Phase 9)

`patchfrog/feedback/` lets PatchFrog learn *operationally* from developer
reactions to published findings -- attribution, measurement, and
auditability. It does **not** retrain anything, rewrite prompts, change
severity/confidence, or suppress future findings automatically. Every
tuning decision this produces evidence for is still made by a human.

## Core principle

**Feedback is noisy evidence, not ground truth.**

- A thumbs-down does not mean "the finding was wrong."
- A resolved GitHub thread does not mean "the finding was correct."
- A code change does not mean "the developer accepted the suggestion."

Every signal is interpreted through explicit, auditable rules in
`patchfrog/feedback/assessment.py` -- never a weighted ML score, never an
LLM judge. See `FeedbackAssessment.reasons` on any computed assessment
for exactly which raw signals produced it.

## What signals mean (and don't)

| Signal | Moves `usefulness_signal` | Moves `correctness_signal` |
|---|---|---|
| `+1` / `heart` / `hooray` reaction | yes, positive | **never** |
| `-1` / `confused` reaction | yes, negative | **never** |
| `laugh` / `rocket` reaction | **never** (uninterpreted) | never |
| `eyes` reaction | no (neutral/attention) | never |
| Generic reply | no | no -- only sets `engagement_signal` |
| `/patchfrog useful` | yes, positive | no |
| `/patchfrog false-positive` | yes, negative | yes, negative |
| `/patchfrog fixed` | no | yes, positive |
| Thread resolved | no | **never** -- only `resolution_signal` |
| Finding disappeared after a later commit | no | yes, positive (medium confidence, "not confirmed") |
| Finding unchanged after a later commit | no | only combined with a negative reaction, and even then only a signal |

A single weak signal (one reaction, one resolved thread) never flips a
finding into a confirmed verdict. `is_false_positive_candidate` /
`is_high_value_candidate` (`patchfrog/feedback/assessment.py`) produce
**candidates**, never `confirmed_false_positive` /
`guaranteed_true_positive` -- see spec sections 25/26.

## Explicit commands

A developer reply whose entire trimmed body is exactly one of:

```
/patchfrog useful
/patchfrog false-positive
/patchfrog fixed
/patchfrog ignore
```

is parsed as a structured, strong-confidence signal
(`patchfrog/feedback/commands.py`). No arguments, no other tokens, exact
vocabulary only. A command embedded in a longer sentence, inside a code
fence, inside a blockquote, or followed by extra text (including
shell-style injection like `&&`) is never parsed as a command -- it's
just a normal reply. Bot actors (`user.type == "Bot"` -- this covers
PatchFrog's own bot identity) are filtered out before any reply or
reaction ever reaches this parser or is recorded as feedback at all.

## Permissions audit (why sync, not webhooks)

PatchFrog's GitHub App currently has:

- `contents: read`
- `metadata: read`
- `pull_requests: write`
- Subscribed webhook events: `pull_request` only.

Given this:

- **Reactions** have no GitHub webhook event at all, for any App
  configuration -- polling (`GET .../pulls/comments/{id}/reactions`) is
  the only way to observe them, ever.
- **Replies** are ordinary PR review comments
  (`GET .../pulls/{number}/comments`, with `in_reply_to_id` set) --
  real-time delivery would need a `pull_request_review_comment` webhook
  subscription PatchFrog does not currently have. Polling the same
  existing `pull_requests` permission works today with zero
  reconfiguration.
- **Thread resolution** has no REST field at all; it's GraphQL-only
  (`reviewThreads.nodes.isResolved`). GraphQL access uses the exact same
  installation token and permission scope as REST -- no new grant needed,
  just a different endpoint.
- **PR merged/closed** is visible on the existing `get_pull_request` call
  (`state`, `merged`).

Given all of the above are fully achievable with the App's *current*
permission set, Phase 9 deliberately does **not** request any new
permission or webhook subscription. Every ingestion path in
`patchfrog/feedback/sync.py` is a poll, run on demand via
`python -m patchfrog.cli feedback sync --repository owner/repo --pr N`.
There is no automatic trigger -- exactly like every other production
write surface in PatchFrog before it.

## Finding attribution

Every feedback event tries, in order:

1. `github_comment_id` already persisted on the publication comment.
2. Deterministic `(path, line, side, body_hash)` matching against
   PatchFrog's own top-level comments for that publication -- this *is*
   `github_comment_id` enrichment (`patchfrog/feedback/attribution.py`),
   run on demand during sync, so a historical row missing
   `github_comment_id` (the pre-Phase-9 gap: the column existed but was
   never populated) self-heals the next time sync runs against its PR.
3. Ambiguous -> no attribution, ever. The event is still recorded (never
   discarded) but excluded from any per-finding summary.

**A real bug found and fixed during this phase's own testing**:
`ReviewPublicationCommentModel.side` stores PatchFrog's *internal*
`DiffSide` vocabulary (`"old"`/`"new"`), while a real GitHub comment
fetched back from the API always carries GitHub's wire vocabulary
(`"LEFT"`/`"RIGHT"`). Comparing them directly (the first version of this
matcher) meant `github_comment_id` enrichment matched *nothing*, ever --
caught by a real-data integration test, fixed with an explicit
translation step, and covered by a regression test.

## Idempotency, ordering, and reaction removal

Every raw event has a stable external identity
(`source, event_type, external_event_id` -- unique in the database).
Re-running sync, or a retried webhook redelivery in the future, can never
create a duplicate row. Since GitHub exposes only *currently active*
reactions (no history), sync reconciles the active set against
previously-ingested `REACTION_ADDED` events on each run and synthesizes a
`REACTION_REMOVED` event for anything no longer present -- a removed
thumbs-up never stays "active" forever. Events are ordered by
`occurred_at`, never by ingestion order.

## Privacy

Only an actor's GitHub login and whether it is a bot are ever persisted
-- no profile, no cross-repository correlation, no reputation score.
Feedback belongs to a repository/PR/finding, never to a "developer
reputation." `patchfrog.cli feedback export` never includes actor
logins, reply bodies (unless explicitly requested with
`--include-reply-bodies`, which today has nothing to include since reply
text is never persisted at all), or raw evidence text -- only a hash of
the finding's evidence.

## Phase 8 vs. Phase 9 metrics -- never mixed

Phase 8 (`patchfrog/evaluation/`) measures PatchFrog against
human-authored ground truth (`benchmark_ground_truth`). Phase 9
(`patchfrog/feedback/metrics.py`) measures how real developers reacted to
what actually got published (`production_feedback`). These are
deliberately separate report shapes, computed by separate code paths,
surfaced through separate CLI commands (`eval` vs. `feedback`). Mixing
them into one precision/recall number would make that number mean two
different things depending on which report you're reading.

## CLI

```
python -m patchfrog.cli feedback sync --repository owner/repo --pr 123
python -m patchfrog.cli feedback list --repository owner/repo [--pr 123] [--positive|--negative|--explicit] [--since ISO8601]
python -m patchfrog.cli feedback show --finding <ai_findings.id>
python -m patchfrog.cli feedback summary [--repository owner/repo]
python -m patchfrog.cli feedback export --output feedback.jsonl [--repository owner/repo] [--include-reply-bodies]
```

`sync` is the only command that talks to GitHub; the rest are read-only
queries over already-ingested data.

## Versioning and recomputation

`FEEDBACK_ENGINE_VERSION` (ingestion behavior) and
`FEEDBACK_ASSESSMENT_VERSION` (interpretation rules) are independent,
bumped whenever their respective logic changes materially. Raw
`feedback_events` rows are immutable and never carry an assessment
version. `feedback_assessments` is fully derived and recomputable
(`patchfrog.feedback.queries.recompute_and_persist_all`) -- changing the
assessment rules and recomputing never rewrites or deletes a single raw
event; it only replaces the derived summary for the current version.

## What Phase 9 deliberately does not do

- No automatic prompt, model, threshold, or category-policy mutation --
  ever. Findings with strong negative feedback surface as
  `false_positive_candidates` for a human to review, never a silent
  suppression rule.
- No comment-sentiment classification via an LLM. A reply's presence is
  `developer_engaged`; its content is never fed back into any reviewer
  prompt.
- No webhook-triggered automation (see the permissions audit above) --
  sync is always an explicit, on-demand action.
- No bulk historical crawl of a repository's existing comments without an
  explicit, scoped sync call naming a specific PR.

## Telemetry integration

`docs/telemetry-intelligence.md` covers how feedback is folded into a
review run's telemetry snapshot (`FeedbackTelemetry`, one entry per
published finding, `has_feedback=False` meaning "unknown" — never
"confirmed correct") and the coverage/useful/user-reported-false-
positive/fixed rate calculations, every one of them denominated by
feedback-*bearing* findings only, never all published findings. Feedback
metrics are never combined with benchmark precision/recall into one
score.
