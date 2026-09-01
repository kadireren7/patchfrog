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
  a single bounded validation session).
- The dogfood PR was **not** merged; it was closed and its branch
  deleted after this summary and the telemetry exports were captured.
