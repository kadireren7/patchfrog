# Merge Readiness (Milestone V)

`patchfrog.merge_readiness` is a synthesis layer over PatchFrog's own
already-persisted evidence. It answers: "given everything PatchFrog
knows about THIS exact PR head, what action does the evidence support?"
It does **not** generate findings, invent a numeric score, replace
GitHub branch protection, guarantee correctness, automatically merge, or
automatically approve.

## Decision

Exactly three outcomes (`MergeReadinessDecision`), deliberately no
`UNKNOWN` -- `HUMAN_REVIEW_REQUIRED` already honestly represents every
case PatchFrog cannot safely decide:

- **`READY`** -- PatchFrog completed the required review for this exact
  head and found no currently unresolved evidence that requires blocking
  or human escalation under the current policy. **Never** "PatchFrog
  guarantees this code is correct."
- **`BLOCKED`** -- at least one strong, concrete, unresolved blocking
  finding exists at this exact head.
- **`HUMAN_REVIEW_REQUIRED`** -- PatchFrog cannot safely decide READY or
  BLOCKED (stale/incomplete review, or a high-impact finding's status is
  genuinely ambiguous). An expected, healthy outcome, not a failure mode
  to eliminate.

**No numeric score of any kind** -- no `risk_score`, `merge_score`,
`confidence_percentage`, or 0-100 value anywhere in the domain
(`test_result_has_no_numeric_score_field` asserts this structurally).

## Exact-head binding

Every result is computed fresh, bound to the PR's *current* `head_sha` at
call time -- `patchfrog.merge_readiness.service.MergeReadinessService.evaluate`
never returns a cached/persisted result for a stale head; a decision for
SHA A is never shown as the decision for SHA B. **Not persisted as a new
table** -- deterministically recomputed on demand from already-persisted
state (`pull_requests`, `review_runs`, `ai_findings`,
`feedback_assessments`, `fix_attempts`), all already indexed for this
exact access pattern. This was an explicit audit decision (Part AL:
"avoid a new table if it can be deterministically recomputed cheaply") --
recomputing on every call also structurally rules out ever serving a
stale cached decision.

## Decision precedence (highest first)

1. **No review run exists at the PR's exact current head at all** (never
   reviewed at this SHA, or the head moved since the last review) ->
   `HUMAN_REVIEW_REQUIRED` / `STALE_REVIEW`.
2. **A review run exists at this exact head but did not finish**
   (`RUNNING`) or a required step failed (`FAILED`/`PARTIAL`) ->
   `HUMAN_REVIEW_REQUIRED` / `REVIEW_INCOMPLETE`. This is the critical
   distinction this milestone's own spec calls out explicitly: "no
   findings because the review didn't run" must never look like "no
   findings because it ran and found none" -- `patchfrog.review.domain.ReviewRunStatus`
   already models exactly this (`RUNNING`/`SUCCEEDED`/`PARTIAL`/`FAILED`),
   reused unchanged rather than inventing a new completeness state
   machine.
3. **At least one accepted, still-open CRITICAL/HIGH finding** with no
   same-exact-head `FIXED` `FixAttempt` and no human dismissal ->
   `BLOCKED` / `UNRESOLVED_BLOCKING_FINDING`.
4. **A would-be-blocking finding's only exact-head fix evidence is
   inconclusive or still in flight**, or a MEDIUM-severity SECURITY
   finding remains open -> `HUMAN_REVIEW_REQUIRED` /
   `HIGH_IMPACT_INCONCLUSIVE` or `BLOCKING_FIX_NOT_VERIFIED`.
5. Otherwise -> `READY` / `NO_UNRESOLVED_EVIDENCE`.

## What counts as "still open"

Every `AIFindingModel` row for the exact-head run -- these already
survived validation, the critic, confidence aggregation, and dedup (see
that model's own docstring), so every row is real accepted evidence
regardless of whether it was actually *published* inline vs.
summarized (GitHub comment-volume budgeting governs presentation, not
evidence truth) -- **unless**:

- A `FeedbackAssessment` exists for it with `resolution_signal ==
  ResolutionState.CLOSED` (a human explicitly dismissed/resolved it,
  reusing the existing feedback pipeline's own authoritative signal,
  never a new dismissal mechanism), or
- A `FixAttempt` for that *exact same finding*, at
  `candidate_fix_commit_sha == this PR's current head_sha` (never any
  other SHA), resolved `FIXED`.

`STALE`/`ERROR` fix attempts never clear or escalate anything (T's own
explicit rule: "STALE: cannot clear anything. ERROR: cannot clear
anything.") -- filtered out entirely, treated as though they didn't
exist. A same-head `STILL_PRESENT` result simply reinforces the
finding's already-blocking status (no different from no fix attempt
existing at all).

## Severity/category policy (precision over recall)

A finding does not automatically block merely by category. LOW/INFO
findings, and MEDIUM findings outside SECURITY, never block or escalate
on their own. A MEDIUM SECURITY finding escalates to
`HUMAN_REVIEW_REQUIRED` (not `BLOCKED` outright -- concrete enough to
need a human look, not concrete enough for PatchFrog to block alone). A
high-confidence CRITICAL/HIGH finding blocks regardless of category --
security does not get a lower bar than correctness, nor an automatically
higher one; evidence and severity govern, never the category label by
itself.

## J-R Intelligence is never consulted here

None of Change/Contract/Intent/Test/Historical/Repository-Learnings/
Trajectory/Cross-PR/Cross-Repo Intelligence persists evidence
attributable to a *specific* finding (confirmed by both this milestone's
own audit and T's prior one, `validation/agent_handoff/latest-summary.md`
section 1.2 -- unchanged) -- only whole-review-run aggregates. Merge
Readiness therefore cannot and does not use any of it as blocking or
escalating evidence; it can only ever inform read-only context exposed
elsewhere (T's `FindingHandoff`), never a readiness verdict by itself.

## Exposure surface

**Read-only MCP tool**: `get_merge_readiness(repository_full_name,
pull_request_number)` (`patchfrog.mcp.server`, the same registration
pattern as the four existing T2 tools) -- repository ownership
independently re-validated exactly like every other tool; a
cross-repository pull request returns the identical `"pull_request_not_found"`
shape as a genuinely missing one. No new MCP write tool: no
`merge_pr`, `approve_pr`, or `override_readiness` -- an agent cannot
self-clear a finding, override `BLOCKED`, mark itself `READY`, or change
routing/readiness policy through source code.

**GitHub PR Review summary line: deliberately not wired this round.**
`patchfrog.publishing.planner.PublicationPlanner.build_plan` is
documented as pure -- no database session, no network call -- while
Merge Readiness fundamentally requires a live DB query, and the
publish pipeline's own stale-head detection happens inside/after
`build_plan` runs. Composing these correctly needs dedicated design
attention this already-large combined milestone did not have room for;
see `validation/model_router_merge_readiness/latest-summary.md` section 6
for the full reasoning. The MCP tool is v1's "one concise readiness
surface" (Part AN).

**No new GitHub App permission requested** -- no Check Run, no Commit
Status; the existing single-PR-Review write path (unused for readiness
this round, per above) already covers what a future summary-line
addition would need.

## Agents and coding-agent interoperability

PatchFrog remains the independent judge. An agent that receives a
`FindingHandoff` (T1), attempts a fix, and gets a `FixAttempt` verdict
(T3) can subsequently call `get_merge_readiness` to see the
*consequence* of that verdict -- but cannot self-clear a finding,
override `BLOCKED`, mark itself `READY`, change provider routing, or
change readiness policy through source code (`.patchfrog.yml` has no
readiness-policy surface at all in v1 -- see below).

## `.patchfrog.yml`: no readiness policy surface in v1

Per this milestone's own spec ("If no safe policy surface is needed in
V: do not add one yet"): v1 ships hard platform defaults for severity/
category policy. Repository content cannot disable a blocker, force
`READY`, or otherwise influence a decision -- `MergeReadinessService.evaluate`
takes no repository-controlled input at all.

## Version constants

`MERGE_READINESS_VERSION = 1` (`patchfrog.merge_readiness.domain`) --
justified despite not being durably persisted, since it is exposed
externally on the MCP wire contract (`get_merge_readiness`'s response
`version` field) and must still identify which semantic contract a
caller is looking at. `REVIEW_ENGINE_VERSION`/`FIX_VERIFICATION_VERSION`
are **not** bumped -- Merge Readiness reads their existing outputs, never
changes what they mean.

## Test matrix

`tests/integration/test_merge_readiness_corpus.py` (25 cases): no
pull request, never-reviewed PR, stale head (review at an old SHA),
`RUNNING`/`FAILED`/`PARTIAL` review status, clean succeeded review with
no candidates, LOW/MEDIUM-non-security findings alone, unresolved HIGH/
CRITICAL findings, MEDIUM SECURITY escalation, dismissed findings,
same-head `FIXED`/`INCONCLUSIVE`/`PENDING`/`STALE`/`ERROR`/`STILL_PRESENT`
fix attempts, old-SHA fix attempts not clearing current-head findings,
determinism, exact-head isolation across a simulated head move, no
numeric score field, and the three-member decision enum.

Controlled corpus result: **false READY = 0, false BLOCKED = 0** (not a
global claim about production PRs in general).
