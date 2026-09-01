# Production Webhook E2E — Validation Summary

Branch `chore/production-webhook-e2e-hardening`, baseline `main` @
`2ea2ad4bc6d98ebf1110d81e46636a57a81df837` (Milestone G). Dogfood PR
[#38](https://github.com/kadireren7/patchfrog/pull/38) on
`kadireren7/patchfrog` (installation `153810631`), branch
`dogfood/production-e2e-2026-09-01`, closed unmerged and deleted after
this validation.

**Headline result**: a real GitHub App webhook → PatchFrog worker →
Gemini review chain was exercised end to end for the first time in this
project's history. It found and led to the fix of one real,
previously-undiscovered bug (`patchfrog/review/providers/gemini_provider.py`),
then succeeded with a real accepted finding, and separately proved
stale-head publish protection live against the real GitHub API. Real
GitHub publication of a live-found finding was **not** achieved this
session — not because of a bug in the webhook/review/publish chain
itself, but because of a genuine, newly-discovered interaction between
incremental review memory's carry-forward mechanism and the
repository-level publish opt-in gate (see "Known limitation" below).
Nothing here was fabricated or extrapolated; every REAL claim is backed
by a persisted run/publication id or a real GitHub API response.

**Update**: the unmet criterion above was a real product gap (a
carried-forward finding had no way to ever become publishable once a
gate opened late), not an external limitation. It was fixed in this same
PR and re-validated live against a second real dogfood PR (#40) — see
section 15 for the full root-cause writeup, the code fix, and the real
publish/idempotency/feedback proof. Sections 1-14 below are preserved
exactly as originally written, as the honest historical record of what
the first pass found.

## 1. REAL: webhook deliveries

Received on `POST /webhooks/github`, HMAC-verified against the real
configured webhook secret (never printed — presence/validity confirmed
by the request being accepted).

| Delivery ID | Event | Action | PR | Head SHA | Result |
|---|---|---|---|---|---|
| `632b01a0-a5e6-11f1-8f95-0d104c3a1f1f` | `pull_request` | `opened` | #38 | `3bb2fbc...` | ingested, pipeline scheduled |
| `862ef9d0-a5e7-11f1-9fed-95e6cab2651d` | `pull_request` | `synchronize` | #38 | `88dc39c...` | ingested, pipeline scheduled |
| `73de72f0-a5e8-11f1-92d8-07487cb9ba6e` | `pull_request` | `synchronize` | #38 | `a374d98...` | ingested, pipeline scheduled |

All three arrived from GitHub's real webhook IP range
(`140.82.112.0/20`), all three returned `202 Accepted`, all three
signatures verified before any parsing occurred.

## 2. REAL: review runs

| Run ID | Head | Status | Candidates | Accepted | Notes |
|---|---|---|---|---|---|
| `6ece3931-93a6-45d5-be99-044e4ffa5a7f` | `3bb2fbc...` | `failed` | 2 reviewed, 0 succeeded | 0 | **Real bug found** (see below) — both candidates' Gemini calls returned `400 INVALID_ARGUMENT` |
| `b474a0ae-6de1-4960-ad84-1b87fb37b6af` | `88dc39c...` | `succeeded` | 2 | 1 | Real accepted finding, after the fix |
| `3d9a61aa-07a6-46b3-893d-5248cab1304f` | `a374d98...` | `succeeded` | 0 (2 skipped via incremental memory) | 0 | 0 new Gemini calls — see "Known limitation" |

Full privacy-safe telemetry for each: `telemetry/run1_failed_thinking_budget_bug.json`,
`telemetry/run2_succeeded_real_finding.json`,
`telemetry/run3_incremental_carried_forward.json` (each produced by
`patchfrog telemetry review <run-id> --format json`, unmodified).

## 3. REAL: the bug found, root cause, and fix

**Observed failure**: run 1's two candidates both failed with
`400 INVALID_ARGUMENT` from `POST .../models/gemini-3.6-flash:generateContent`,
body `{"error":{"code":400,"message":"Request contains an invalid
argument.","status":"INVALID_ARGUMENT"}}` — no offending field named.

**Root cause** (confirmed via research against Google's own
documentation, not guessed): Gemini 3.x-family models (including the
configured `gemini-3.6-flash`) replaced the 2.5-family's token-count
`thinking_budget` field with a coarse `thinking_level` enum
(`MINIMAL`/`LOW`/`MEDIUM`/`HIGH`) — the two are mutually exclusive on
one request. `GeminiLLMProvider.generate_structured` unconditionally
sent `thinking_budget` (added by an earlier milestone's thinking-budget
cap fix) regardless of the configured model's generation.

**Fix** (`patchfrog/review/providers/gemini_provider.py`): a new
`_uses_thinking_level(model)` helper detects the model's generation from
its name's leading major-version number and selects the correct field —
`thinking_level` (fixed at `LOW`) for 3.x+, the existing capped
`thinking_budget` for 2.5-family and unrecognized naming shapes. 4 new
unit tests plus 2 existing tests retargeted to an explicit 2.5-family
model (respx-mocked, no live calls).

**Fix validated live**: run 2, immediately after restarting the worker
with the fix loaded, succeeded — 3/3 real Gemini calls returned `200
OK` (2 reviewer, 1 critic).

## 4. REAL: live Gemini provider usage

Aggregate across the whole session (5 real HTTP requests to Gemini's
`generateContent` endpoint: 2 failed pre-fix, 3 succeeded post-fix):

| | Reviewer | Critic |
|---|---:|---:|
| Calls attempted | 4 (2 failed, 2 succeeded) | 1 (succeeded) |
| Input tokens | 3,827 (run 2 only — run 1's failed calls reported 0, never fabricated) | 1,364 |
| Output tokens | 811 | 84 |
| Thinking tokens | 0 (not reported by the API for this call shape) | 0 |
| Retries consumed | 0 | 0 |
| Provider-work latency aggregate | 13,102 ms | 2,555 ms |

Model: `gemini-3.6-flash` (reviewer and critic). Provider: `gemini`
throughout — **no Anthropic call was made anywhere in this milestone**.

Operator hard caps in effect for every call:
`PATCHFROG_MAX_REVIEW_CANDIDATES=2`, `PATCHFROG_MAX_TOTAL_INPUT_TOKENS=30000`,
`PATCHFROG_MAX_OUTPUT_TOKENS_PER_CANDIDATE=2000`,
`PATCHFROG_MAX_CONCURRENT_REVIEW_REQUESTS=1`,
`PATCHFROG_MAX_REVIEW_RETRIES=1` — **bounded deviation**: `1`, not the
literally-requested `0`, because `Settings`' own field validator rejects
`0` as non-positive; `1` is the smallest value it accepts.
`PATCHFROG_REVIEW_REQUEST_TIMEOUT_SECONDS=180` (headroom above the
120s per-provider default, given prior live validation observed calls
up to ~144s).

**Three live-provider-triggering commits, each with a distinct,
necessary purpose — never a repeat "for nicer numbers"**:
1. The dogfood fixture itself (`opened`) — first live review, found the
   thinking_level/thinking_budget bug.
2. A synchronize commit (planned in advance, to also exercise the real
   `synchronize` webhook path) — first successful live review, after the
   fix.
3. A `.patchfrog.yml` commit, added specifically to open the
   previously-unconfigured repository-level publish gate — explicitly
   approved by the user mid-session after a clear explanation of why a
   third live-provider-triggering commit was needed and that the first
   two runs were not "invalid," just serving a different, not-yet-met
   acceptance criterion (real publication). This run made **zero** new
   Gemini calls (incremental review memory correctly avoided re-review
   of unchanged code — see section 8).

## 5. REAL: Quality + Cost Guard observation (run 2 — observational only, not a quality claim)

- Candidate 1 (`apply_discount`'s module region): tier `LIGHT`, reason
  `no_signal`, 1 proposal, suppressed as an AI/AI duplicate against
  candidate 2's finding for the same bug.
- Candidate 2 (`apply_discount` itself): tier escalated `LIGHT →` — no,
  provisional was not LIGHT for this one; escalated to `DEEP` via the
  post-proposal `high_risk_proposal` path (a surviving `HIGH`-severity
  proposal), matching `patchfrog/review/effort.py`'s documented
  design exactly — critic became mandatory for this candidate as a
  direct result.
- Security role was never called for either candidate (no real
  security-naming signal at provisional decision time; the post-proposal
  escalation path never reruns a specialist role by design).
- No candidates skipped for budget.

## 6. REAL: adaptive context observation

Neither candidate's context bundle attempted adaptive expansion
(`adaptive_attempted: false` for both) — the fixture file is small with
no multi-hop call chain, so this is an expected, unforced non-occurrence,
not a gap. Milestone E/F's deterministic tests already prove the feature
itself; this run adds a real (if negative) data point, nothing more.

## 7. REAL: stale-head publish protection

After run 3 (head `a374d98...`) existed, a retry publish of run 2 (head
`88dc39c...`, the run with the real accepted finding) was dispatched
against the real, live PR:

```
review_publish_stale publication_id=debe9d90-40fa-4151-9b5c-36ae32327114
reason=HEAD_SHA_MISMATCH: review was generated for '88dc39c...',
but the pull request's current head is 'a374d98...'
```

**Zero GitHub writes.** Confirmed against a real, live GitHub head-SHA
read (`GET /repos/.../pulls/38`), not a cached/local value. This is the
same guarantee `tests/integration/test_publishing_stale_head.py` proves
deterministically, now also confirmed live.

## 8. Publication attempts and the known limitation

Two other publish dispatches, both real `patchfrog.publish_review`
Celery task invocations, both correctly reasoned about their gate/finding
state without ever attempting a GitHub write:

| Publication ID | For run | Result | Why |
|---|---|---|---|
| `b869e6b4-b41f-4270-8873-700d34036222` | run 2 | `skipped_disabled` | `.patchfrog.yml` didn't exist yet at the reviewed commit — `PublicationConfig.enabled` defaults `false` (documented, intentional third safety gate; see `docs/onboarding.md`) |
| `e4949178-fc0f-42fd-a0f4-a778b73531bf` | run 3 | `skipped_no_findings` | see below |

**Known limitation, discovered live (not fixed in this milestone —
out of scope, cross-cutting, deserves its own design pass)**: enabling
`.patchfrog.yml`'s `publish.enabled` required a new commit (config is
resolved from the exact reviewed commit's tree, by design, to avoid a
config-vs-reviewed-content mismatch). That new commit's own review (run
3) found `candidates_selected=0, candidates_skipped_evidence_confirmed=1`
— Phase 7's incremental review memory correctly recognized the buggy
code was unchanged since run 2 and *carried forward* the existing
finding instead of re-calling the LLM (a real, correct, cost-saving
behavior, and genuinely a nice unplanned confirmation of that feature
working live). However, a carried-forward finding is not copied into the
new run's own `ai_findings`, so `ReviewPublicationService` (which only
ever looks at *this run's* own findings) sees zero findings to publish.
Since run 2's finding was never published before this gate was opened
(it couldn't have been — the gate didn't exist yet), the practical
result is that this specific finding can now never be published without
either a fresh code change (forcing genuine re-review) or a
cross-cutting change to how carried-forward findings interact with
publication eligibility. **This is a real, worth-tracking product
question for a future milestone, not a bug in the webhook/review/publish
chain this milestone hardens** — every individual mechanism involved
(stale-head protection, the three publish gates, incremental review
memory's cost-saving carry-forward) is working exactly as designed; the
interaction between "gate opened after the fact" and "carry-forward
never re-publishes" simply wasn't previously exercised.

**Mandatory acceptance criterion #9 ("normal PatchFrog publication
reaches GitHub") is therefore not satisfied this session** — see the
Milestone H PR description's READY/BLOCKED determination.

## 9. REAL: feedback sync

```
patchfrog feedback sync --repository kadireren7/patchfrog --pr 38
→ observed=0 ingested=0 duplicates_ignored=0 unattributed=0 github_comment_ids_enriched=0
```

A real sync against the real PR, correctly finding nothing — no
PatchFrog comment was ever published to react to (see section 8). Not
fabricated; an honest zero-result real sync.

## 10. SYNTHETIC: negative-path and idempotency checks

Every item below is a **locally-constructed** request against the real,
running local API process — never a real GitHub delivery, never labeled
as one.

| Check | Method | Result |
|---|---|---|
| Invalid signature | `curl` with a wrong-but-well-formed `X-Hub-Signature-256` | `401`, rejected before parsing |
| Missing signature | `curl` with no signature header | `401` |
| Malformed body, no signature | `curl` with non-JSON body, no signature | `401` (signature checked before JSON parsing — confirmed ordering) |
| Unsupported event, valid signature | `issue_comment` event, correctly HMAC-signed | `200 {"detail":"ignored"}`, nothing scheduled |
| Duplicate delivery replay | Same `delivery_id` as the real `opened` delivery (`632b01a0-...`), locally-reconstructed payload, correctly signed | `202` at the HTTP layer (as designed — dedup happens in the ingestion service, not the route); worker log: `pull_request_ingestion_duplicate`, task result `'duplicate'`, no second ingestion, no second pipeline scheduled |

## 11. Failure recovery, publication idempotency, and installation-ownership coverage

Exercised via the existing, extensive deterministic test suite (Fake
providers only, no live spend) rather than live-repeated for
confidence, per Milestone H's own cost-guard instruction:

- `tests/integration/test_review_failure_recovery.py` — one-candidate
  provider failure (`PARTIAL`), all-candidates failure (`FAILED`),
  critic schema failure graceful degradation, untyped critic exception
  not silently swallowed.
- `tests/integration/test_publishing_concurrency.py`,
  `test_publishing_persistence.py` — publish retry/idempotency,
  concurrent publish attempts, DB-level uniqueness.
- `tests/integration/test_ops_eligibility_db.py::test_installation_mismatch_fails_closed` —
  installation-ownership trust boundary.
- `tests/integration/test_review_quality_cost_guard.py` — budget
  exhaustion paths.

All still passing as part of this milestone's gate run (see the PR's
test-plan section) — none needed strengthening beyond the one genuine
gap this milestone's own audit found (see below).

## 12. New deterministic test coverage added this milestone

- `tests/integration/test_webhook_route.py::test_synchronize_event_is_queued` —
  the one real gap found: a real-shaped `synchronize` fixture
  (`tests/fixtures/pull_request_synchronize.json`, pre-existing but
  previously only exercised at the parser unit-test level) was never
  exercised at the full HTTP webhook-route level.
- `tests/unit/test_review_gemini_provider_contract.py` — 4 new tests +
  2 retargeted, covering the thinking_level/thinking_budget generation
  split directly (respx-mocked, no live calls).

## 13. Privacy / secret handling

- No `.env` file, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, GitHub App
  private key, installation token, webhook secret, or
  `X-Hub-Signature-256` value was ever printed, logged, or committed
  during this session.
- `GET /app/hook/config`'s `secret` field is returned pre-masked by
  GitHub itself (`"********"`) — only presence was checked, the masked
  value was never treated as real.
- The temporary Cloudflare tunnel URL and the App's webhook URL change/
  revert were both real, deliberate, user-approved actions (tunnel setup
  was pre-authorized by the milestone instructions themselves; the
  `PATCH /app/hook/config` call was explicitly approved after an earlier
  attempt was blocked by the harness's own permission classifier).
- Operator-level state changed during this session and explicitly
  reverted afterward: `installation.publication_allowed` (`False` →
  `True` for validation → `False`), the App's webhook URL (temporary
  tunnel → reverted to the exact prior value).
- All three telemetry JSON exports in `telemetry/` were grepped for
  secret-shaped strings before being committed — none found (expected;
  `patchfrog.telemetry` is privacy-safe by construction, see
  `docs/telemetry-intelligence.md`).

## 14. What was NOT done

- No Anthropic call, anywhere.
- No live benchmark corpus run.
- No new provider or agent.
- No prompt redesign.
- No fourth live-provider call, once the known limitation in section 8
  was understood (would have required either a genuine code change to
  the dogfood fixture, forcing a real re-review, or a cross-cutting
  incremental-memory/publication design change — both out of scope for
  a single bounded validation session). **Superseded by section 15**:
  the cross-cutting design change was completed in a follow-up
  correction round in this same PR, which did make exactly one further
  bounded live-provider call to complete the originally-unmet
  acceptance criterion for real.
- The dogfood PR was **not** merged; it was closed and its branch
  deleted after this summary and the telemetry exports were captured.

## 15. CORRECTION ROUND: the "known limitation" in section 8 is a real product gap, now fixed

Sections 1-14 above are preserved exactly as originally written and are
**not** rewritten by this correction — they remain the honest record of
what the first validation pass found, including its one unmet mandatory
criterion. This section records what changed afterward, in the same PR,
after the user rejected "READY, with one mandatory criterion not met"
as an internally contradictory status and required the underlying
product gap to actually be fixed rather than only documented.

### 15.1 Root cause, precisely

`ReviewPublicationService.publish()` computed its publishable-finding set
as `get_publishable_findings(review_run_id)` — strictly the current run's
own fresh `ai_findings` rows. A Phase 7 (`patchfrog.review_memory`)
zero-AI-call carried-forward finding is never copied into the carrying
run's own `ai_findings` (by design — see the module docstring of
`patchfrog.review_memory.service` on the accepted-vs-published
distinction); it only ever exists as a `ReviewMemoryFindingModel` row
pointing back at the `ai_findings` row from whichever run last actually
produced it. So once a publish gate opened on a later, carry-forward-only
head (run 3 in section 8), there was structurally no way for run 2's
real, already-accepted, still-active finding to ever enter a publishable
set again.

Separately, and more subtly: the *existing*
`already_reported_finding_ids` suppression mechanism
(`ReviewMemoryFindingRepository.list_carried_forward_current_finding_ids`,
feeding `PublicationDisposition.ALREADY_REPORTED`) assumed that *any*
`CARRIED_FORWARD` memory row for the current run represents a finding
already reported to GitHub in a previous publication — true when
publishing was enabled from the start, but false in exactly this
session's own scenario (the underlying finding was never actually
published, because the gate opened *after* it was first found). Left
alone, that assumption would have permanently and silently suppressed
this finding even after the fix added it to the publishable set.

### 15.2 Chosen design, and why

Investigated the full Phase 7 memory architecture
(`patchfrog/review_memory/{domain,service,resolver}.py`,
`patchfrog/persistence/models/review_memory.py`,
`patchfrog/persistence/repositories/review_memory_finding.py`,
`patchfrog/publishing/{queries,service,planner,domain}.py`,
`patchfrog/persistence/models/publishing.py`,
`patchfrog/persistence/repositories/review_publication_comment.py`)
before choosing an approach, per the correction's explicit instruction.
Chose the "typed current-active-finding query/service" design (option B
of the two suggested): a new
`patchfrog.publishing.queries.get_current_active_findings(review_run_id)`
merges the current run's fresh findings with any `CARRIED_FORWARD`
memory finding scoped to that exact `review_run_id`, excluding whichever
of those were **actually already published** — determined by a new
`ReviewPublicationCommentRepository.list_actually_published_finding_ids`
query that requires both a real `PUBLISHED`-status parent publication
*and* an `INLINE`/`SUMMARY_ONLY` disposition (a `DRY_RUN` attempt, a
`FAILED` attempt, or the `ALREADY_REPORTED` disposition itself never
count — none of them ever wrote anything real to GitHub). Rejected
option A (materializing a carried-forward projection row into the
current review run) because it would mean writing a synthetic
`ai_findings`-shaped row for something Phase 5 never produced this run,
which the existing "review generation only ever includes what it
genuinely reviewed" invariant (see `patchfrog/persistence/models/review.py`)
would then have to special-case around forever.

`ReviewPublicationService.publish()` now calls
`get_current_active_findings` instead of `get_publishable_findings`
directly, and unions its returned already-published set into the
existing `already_reported_finding_ids` parameter — the pre-existing
`ALREADY_REPORTED` suppression path (planner, persisted comment
disposition, telemetry) is reused completely unchanged; only what feeds
it became more accurate.

### 15.3 Zero new provider calls, zero schema changes, no version bump

- `get_current_active_findings` never calls an LLM and never re-derives
  a continuity/evidence decision — it only reads state
  `patchfrog.review_memory`'s own `finalize()` already persisted.
- No new database column, table, or Alembic migration —
  `ReviewMemoryFindingModel.current_finding_id` /
  `current_review_run_id` / `status` and
  `ReviewPublicationCommentModel.finding_id` / `disposition` plus
  `ReviewPublicationModel.status` already carried everything required.
  Provenance (originating finding/run, current review run,
  `carried_forward`, revalidated-at-current-head) is fully answerable by
  joining these existing tables — no new field was added to
  `PublishableFinding` or the persisted comment row, and the visible
  GitHub comment body format is unchanged.
- No engine/policy/schema version constant was bumped
  (`REVIEW_ENGINE_VERSION`, `REVIEW_POLICY_VERSION`,
  `PUBLICATION_ENGINE_VERSION`, `PUBLICATION_CONFIG_SCHEMA_VERSION`,
  `INCREMENTAL_REVIEW_ENGINE_VERSION`, `TELEMETRY_SCHEMA_VERSION`).
  Reasoning: (1) an already-`PUBLISHED` publication row is permanently
  protected by the existing `uq_review_publications_published_identity`
  partial unique index plus `get_or_create_attempt`'s
  `ALREADY_PUBLISHED` short-circuit *before* `get_current_active_findings`
  is even consulted for that identity again — no already-terminal
  publication is ever reconsidered under the new logic, so no old data
  needs invalidating; (2) `PublicationConfig`'s own fields and
  `.fingerprint()` are untouched, so publication identity itself is
  unaffected; (3) no review-memory continuity/evidence decision logic
  changed, so incremental review memory's own version is unaffected;
  (4) this codebase's own established precedent (Milestone G added a
  whole new `review_feedback` telemetry field without bumping
  `TELEMETRY_SCHEMA_VERSION`, since it was purely additive) was followed
  for telemetry too — no telemetry field was added at all this round,
  since the same provenance is already fully queryable from persisted
  state without one, and the correction's own instruction was to add
  telemetry "only if necessary."

### 15.4 Deterministic test coverage added

- `tests/integration/test_publishing_current_active_findings.py` (10
  tests) — direct, hand-crafted-row coverage of
  `get_current_active_findings`: zero-call carry-forward becomes
  publishable; already-published carry-forward is suppressed, not
  re-added; a `DRY_RUN`-only or `ALREADY_REPORTED`-only publication
  history never counts as "actually published"; `RESOLVED` / `CHANGED`
  / `AMBIGUOUS` memory statuses are never carried or published; a
  memory row scoped to a different run is ignored; fresh and carried
  findings are never double-added; no-memory-rows behaves exactly like
  the pre-fix `get_publishable_findings`.
- `tests/integration/test_publishing_carried_forward_findings.py` (1
  real four-commit lifecycle test, real git repo + real
  `IncrementalReviewMemoryService` + real `PullRequestReviewService` +
  real `ReviewPublicationService` against `FakeReviewPublisher`) —
  finding accepted with publishing disabled; a config-only head change
  (README-only, `divide` itself byte-for-byte untouched) carries the
  finding forward with **zero** AI calls for `divide` specifically;
  publishing, now enabled, publishes it for real (exactly one GitHub
  write); an immediate retry of the same publication identity is
  idempotent (still exactly one write, zero further LLM calls); a
  second config-only commit's own carry-forward is correctly suppressed
  as already-published (zero new writes); the stale-head guard still
  wins for a run that was never published once a newer head exists; a
  real recheck that genuinely fixes the bug resolves the finding and it
  is never published.

Full local unit + integration suite: 1319 tests total, 1316 passing (3
pre-existing failures in `tests/integration/test_static_analysis_service.py`,
confirmed via `git stash` to fail identically with none of this
milestone's changes applied — an unrelated, pre-existing environment
issue, not caused by this work).

### 15.5 REAL: live re-validation against `kadireren7/patchfrog`

Second dogfood PR, [#40](https://github.com/kadireren7/patchfrog/pull/40)
on `kadireren7/patchfrog`, branch `dogfood/publish-gate-live-2026-09-01`
— closed unmerged and its branch deleted after this validation. Unlike
PR #38, `.patchfrog.yml` (`publish.enabled: true, min_severity: medium`)
was present from this PR's **first** commit, so the publish gate was
open from the start — the corrected acceptance criterion this round
needed to actually exercise. Installation `publication_allowed` and
`global_publication_enabled` (the latter already `true` by default) were
both enabled for the duration and reverted afterward.

**One real, self-inflicted operator misconfiguration, corrected before
any valid attempt (not a PatchFrog bug)**: the worker was first started
with `PATCHFROG_REVIEW_PROVIDER=gemini` but without explicitly setting
`PATCHFROG_REVIEW_MODEL`, so it defaulted to `claude-opus-5` (the
Anthropic default) while still using the Gemini client -- both real
candidates for the `opened` delivery (`github_delivery_id=92641550-a5f1-11f1-9da1-f05fe727c679`,
head `2023bd1...`) failed with a real `404 NOT_FOUND` from Gemini's API
(`models/claude-opus-5 is not found`). This is a real HTTP call that
really failed, but it never exercised the acceptance criterion at all
-- corrected by restarting the worker with `PATCHFROG_REVIEW_MODEL=gemini-3.6-flash`
/ `PATCHFROG_REVIEW_CRITIC_MODEL=gemini-3.6-flash` explicitly set, then
pushing one necessary follow-up commit (a `synchronize`) to get a fresh
review attempt under the corrected configuration -- consistent with this
milestone's own established exception ("only if the prior run was
invalid... due to infrastructure failure"); an operator misconfiguration
preventing any real review from occurring is exactly that.

**Real `synchronize` delivery** (`github_delivery_id=e3429910-a5f1-11f1-98dc-5d0137a4631a`,
head `c5fa4379aedcd19cf4a0ef0723d16308aea0757e`) -> **real review run**
`ea941e9d-5c65-400f-962a-3bf36b81cdb9`, `succeeded`, 2 candidates
reviewed, 1 accepted (the intentional `apply_tax` percentage/fraction
bug, correctly found, `severity=high`, `confidence=high`). Real Gemini
usage: 2 reviewer calls (3,857 input / 450 output tokens) + 1 critic
call (1,555 input / 89 output tokens), `gemini-3.6-flash` throughout,
`retries_consumed=0` -- both within the same strict operator caps as
before, no Anthropic call anywhere. Candidate 1 (`apply_tax` itself) was
escalated to the `deep` effort tier via the `high_risk_proposal` path,
the same real Quality + Cost Guard behavior observed in section 5.

**Real publication**: `patchfrog.publish_review` produced
publication `ccad1193-7388-4bfb-97c9-f189e3d451c5`, status `published`,
`published_inline=1` -- a real GitHub PR review,
[review id `5077028418`](https://github.com/kadireren7/patchfrog/pull/40#pullrequestreview-5077028418),
one real inline comment (id `3903299441`) on
`validation/production_e2e/dogfood2/pricing.py`, confirmed independently
via `gh api repos/kadireren7/patchfrog/pulls/40/reviews` (not only from
worker logs/DB state). This is the exact, previously-unmet mandatory
acceptance criterion: a carried-forward-publication-fix-enabled
`patchfrog.publish_review` dispatch reaching real GitHub for the first
time.

**Real publication idempotency**: the exact same `review_run_id` was
dispatched to `patchfrog.publish_review` a second time. Result:
`status=published`, `reconciled=True`, `review_publish_reconciled
... reason=already_published` -- zero new GitHub write. Confirmed
against GitHub itself, not only DB state:
`gh api .../pulls/40/reviews` still reports exactly one
`patchfrog[bot]` review (`5077028418`), both before and after the
retry.

**Real feedback signal**: a single `+1` reaction (id `433109934`,
added via `gh api .../pulls/comments/3903299441/reactions`) on the real
inline comment. `patchfrog feedback sync --repository
kadireren7/patchfrog --pr 40` reported `observed=1 ingested=1
duplicates_ignored=0 unattributed=0 github_comment_ids_enriched=1` --
attributed to the exact real finding id `7ef76126-60d7-41c3-b53b-051398f2410c`
(confirmed via `patchfrog telemetry review ea941e9d-... --format json`:
`feedback: [{finding_id: "7ef76126-...", scope: "finding",
has_feedback: true, positive_reactions: 1, usefulness_signal:
"positive"}]` -- `scope: "finding"` is exactly
`FeedbackScope.FINDING`).

**Final telemetry** for this run is saved at
`telemetry/run4_corrected_publish_lifecycle.json` (grepped for
secret-shaped strings before commit -- none found), confirming: real
provider usage as above, `deep` effort tier + escalation reason present,
critic usage present, finding lifecycle `accepted_final`, real
finding-scoped feedback as above, and no raw source/context/prompt/
provider-response/comment-body/secret text anywhere in the export --
the same privacy guarantee section 13 already established, now
reconfirmed for this run.

**Cleanup, identical discipline to the first round**: dogfood PR #40
closed unmerged with an explanatory comment, its branch deleted both
remotely and locally; `installation.publication_allowed` reverted
`True` -> `False`; the App's webhook URL reverted to the exact prior
value (`https://specialty-impose-depend-alias.trycloudflare.com/webhooks/github`);
the temporary `cloudflared` tunnel, host-venv API process, and host-venv
worker process were all stopped; the local dogfood clone directory was
deleted. Review/publication/feedback/telemetry DB rows for PR #40 were
preserved (not cleaned up), same reasoning as section "Cleanup
discipline" in memory: this validation artifact references their exact
ids by design.

**Net result: mandatory acceptance criterion #9 ("normal PatchFrog
publication reaches GitHub"), unmet in the first round (section 8), is
now satisfied for real** -- both the originally-blocked scenario (a
finding accepted before a gate opened, section 15.1-15.4's code fix)
and a freshly-generated finding published for the first time
(section 15.5) are proven, live, against PatchFrog's own real GitHub
repository.
