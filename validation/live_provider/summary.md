# Live Anthropic Provider Validation — Summary

Branch `chore/live-anthropic-validation`, baseline `main` @ `4fa4a777c297e997cfc067b4365e4b3cb6198688`.

**Headline result: real, live validation against the production Anthropic provider
was performed for the first time in this project's history, and it found and led
to the fix of one real, previously-undiscovered bug in `patchfrog/publishing/body.py`.
Full-scope validation (74-case benchmark, security-quality corpus, critic/context
ablation, real webhook E2E, feedback loop) was cut short partway through by the
configured Anthropic account running out of credit balance — an external/billing
constraint, not a PatchFrog defect.** Everything reported below is either a real
API result or explicitly labeled as not attempted; nothing was fabricated or
extrapolated from mocked data.

## 1. Provider identity (production defaults, none hand-picked)

| | |
|---|---|
| Provider | `anthropic` |
| Reviewer model | `claude-opus-5` |
| Critic model | `claude-opus-5` |
| Critic enabled | `true` (production default) |
| `REVIEW_PROMPT_VERSION` | 2 |
| `REVIEW_POLICY_VERSION` | 2 |
| `REVIEW_ENGINE_VERSION` | 1 |
| `ReviewConfig.CONFIG_SCHEMA_VERSION` | 1 |
| `CONTEXT_ENGINE_VERSION` | 1 |
| `PARSER_VERSION` | 2 |
| `COMMENT_FORMAT_VERSION` | 3 |
| `PUBLICATION_CONFIG_SCHEMA_VERSION` | 2 |
| `PUBLICATION_ENGINE_VERSION` | 1 |

## 2. Direct live provider smoke test — PASSED

Using `provider_factory.build_reviewer_provider` + the real `build_reviewer_prompt`
+ `REVIEW_RESPONSE_SCHEMA` (production code, not a hand-rolled schema) against a
synthetic plaintext-password-logging bug:

- Auth succeeded, request succeeded, structured output parsed, schema validated.
- 1 finding returned: `HIGH`/`security`, code-grounded identification, root
  cause, impact, and fix — matches the Security Review Quality tone exactly
  (no generic advice, no raw confidence number).
- `input_tokens=3636 output_tokens=590 latency_ms=11728`.

## 3. Live redaction check — PASSED

Ran the real `patchfrog.cli review` CLI path (full production pipeline: index →
analyze → context → live reviewer → live critic → persistence) against a
synthetic repo. Inspected both the structured JSON logs and the raw `httpx`
transport log:

- No API key, no `Authorization` header, no `sk-ant-*` value anywhere in either.
- Token-usage fields (`reviewer_tokens=7286/1358 critic_tokens=3991/415`)
  **remained visible**, confirming the redaction fix from the prior session
  (`chore/live-runtime-enablement`) works correctly under real traffic, not just
  in unit tests.
- One transient `503 Service Unavailable` was hit and auto-retried successfully
  by the existing retry policy — real-world resilience observed, not injected.

## 4. Live Phase 8 benchmark — **PARTIAL** (13/74 cases; credit exhaustion)

`patchfrog.cli eval run --provider live --incremental-scenarios` was launched
against the full 74-case corpus. **The configured Anthropic account ran out of
credit balance partway through** (every call from that point returned
`400 invalid_request_error: "Your credit balance is too low..."`). The run was
stopped once this was confirmed, rather than continuing to burn through 400s
against every remaining case. See `benchmark_partial.json` for raw data.

13 cases got genuine (non-zero-token) results before the wall was hit — 9 bug
cases, 4 clean cases — manually cross-checked against each case's `case.yaml`
ground truth (no automated harness report was produced, since the run never
reached its own summary step):

| Metric (13-case benchmark partial sample) | Result |
|---|---|
| Bug cases attempted | 9 |
| Target bug recall on completed bug cases | 9 / 9 |
| Missed target bugs | 0 |
| Hallucinated FP *in this 13-case sample* | 0 |
| Near-duplicate extra reports (same bug re-reported) | 3 (`c-double-free` ×2 extra, `c-memory-leak` ×1 extra) |
| One additional, real, different finding (not in ground truth) | 1 (`c-file-handle-leak`: a second genuine issue, `out_size=0` passed to `fgets`) |
| Clean cases attempted | 4 |
| Strict clean zero-finding pass | 2 / 4 (`clean-c-correct-realloc-pattern`, `clean-c-manual-memory-valid`) |
| Clean cases that produced a real-but-out-of-scope finding | 2 / 4 (both flagged genuine integer-overflow-in-allocation-size patterns — see §5) |

**This 0-hallucination figure is scoped strictly to these 13 benchmark cases —
it is not a global claim.** A separate, unsupported repository-level claim
*was* observed elsewhere in this session's live validation, in the independent
context-ablation run against a different fixture (§8): the model asserted a
macro wasn't defined in a header when it in fact was, because that part of the
header was omitted from its `ContextBundle`. That finding belongs to a
disjoint population from this benchmark sample and is not double-counted here
or excluded from the report — see §8 for the full classification. No
precision/recall/F1 figure is reported as a completed benchmark result: the
full 74-case harness never finished, so nothing here should be read as
"the canonical Phase 8 precision/recall number."

## 5. Clean-case finding review (manual, per case)

- **`clean-c-correct-bounds-loop`**: flagged `sum_array`'s `int total` as
  overflow-prone for large arrays. Verified against source: **real, correct
  observation** — genuinely out of scope for what this fixture's ground truth
  tests (loop-bound correctness), but not a hallucination. Classification:
  `correct_but_out_of_scope`.
- **`clean-c-defensive-null-check`**: flagged `count * sizeof(int)` in
  `buffer_create` as an unchecked overflow in an allocation-size computation
  (a real CWE-190 pattern). Verified against source: **real, correct
  observation**, same classification.

Neither finding invented a fact about the code; both are the kind of secondary
observation a careful human reviewer might also raise. This is a case where the
benchmark's binary "clean" label (zero defects of the one specific kind under
test) and "the model found *a* real defect" are both true at once — worth
noting as a benchmark-design nuance, not a quality regression.

## 6. Live security-quality corpus — **NOT RUN** (credit exhaustion)

Blocked before it could be started. `LIVE_PROVIDER_CREDIT_EXHAUSTED`.

## 7. Live critic ablation — **NOT RUN** (credit exhaustion)

Blocked before a dedicated ablation subset could be run.
`LIVE_PROVIDER_CREDIT_EXHAUSTED`. (Critic-ON data exists incidentally in every
result above, since production defaults to `critic_enabled=True`; no critic-OFF
comparison was obtained.)

## 8. Live context ablation, extern case — **RUN, root cause identified**

Re-ran `validation/private_beta/cases/case11-cross-file` (the fixture the
extern-cross-file-resolution fix, PR #25, was built to address) live. Result:
**the specific expected finding (`connectOnStartup` violating
`RETRY_POLICY_MAX_ATTEMPTS`) was still missed**, but for a *different*, newly
identified reason than before the fix:

- The context bundle for the `connectOnStartup` candidate correctly included
  `reconnectWithBackoff`'s real cross-file definition (confirming the extern
  *symbol-resolution* fix from PR #25 is working).
- It did **not** include `retry_policy.h`/`computeBackoffMs` — the actual
  contract constant lives **two hops** away
  (`connectOnStartup` → `reconnectWithBackoff` → `computeBackoffMs`), and the
  Context Engine's bundle only walks **one hop** of direct callees (confirmed
  by querying `context_items` directly: only the target symbol, its direct
  callee, and one same-file adjacent symbol were included).
- The model, given a truncated view, correctly reasoned about what it *could*
  see, but one of its resulting findings makes a claim that is **false against
  the actual repository**: it stated `retry_policy.h` does not define
  `RETRY_POLICY_MAX_ATTEMPTS`. Direct repository inspection shows the macro
  **is** defined there (`#define RETRY_POLICY_MAX_ATTEMPTS 5`). This is
  classified precisely, not softened:

  - `unsupported_repository_claims_observed`: **1**
  - `root_cause`: relevant source (the macro definition + its CONTRACT
    comment) omitted from this candidate's `ContextBundle` — confirmed by
    querying `context_items` directly, which shows only an
    include-guard-range slice of the header for this candidate
  - `model_reasoning_given_supplied_context`: plausible — the model's stated
    reasoning ("the header as shown contains only the include-guard") is an
    accurate description of what it was actually handed
  - `repository_ground_truth`: incorrect
  - This is a **repository-level unsupported/incorrect claim caused by
    incomplete context**, not counted in and not double-counted against §4's
    13-case benchmark sample (a disjoint population — see §4's note).

  A second finding in this same run ("unbounded shift in `computeBackoffMs`
  overflows int") is real and technically valid about that function in
  isolation, just not reachable given the actual caller's bound of 20 —
  `correct_but_out_of_scope`, not an unsupported claim.

**This is a real, previously-undocumented Context Engine limitation** (call-graph
context depth is 1 hop, not transitive) — reported here as a finding for a
future session to weigh (extending traversal depth has real token-budget and
noise tradeoffs; not a "smallest fix," so no code change was made for it in
this session per the bug-fix policy's scope discipline).

## 9. Natural PR dry-runs — 1 / 4 complete (credit exhaustion)

4 real, already-merged PatchFrog PRs were reviewed via `git worktree` +
`patchfrog.cli review` (no CLI publish, matching "dry-run"):

| PR | Result |
|---|---|
| #27 (review branding) | **Complete** — 26/33 candidates reviewed before the credit wall hit mid-run; 4 accepted findings |
| #25 (extern fix) | Blocked — 0 tokens, immediate credit-exhaustion error |
| #28 (brand assets) | Blocked — 0 tokens, immediate credit-exhaustion error |
| #29 (live runtime enablement) | Blocked — 0 tokens, immediate credit-exhaustion error |

**PR #27's 4 findings, human-classified:**

1. & 3. "Omitted-findings footer names PatchFrog a second time" (2 near-duplicate
   reports of the same real, low-severity docstring/footer-wording
   inconsistency) — `correct_but_low_value`.
2. **"Untrusted `title` can contain newlines, breaking the single-line
   header"** — verified as a **real, previously-undiscovered bug**: `useful_correct`.
   See §17 (Bugs found/fixed).
4. "Docstring invariant broken when `omitted_count > 0`, untested" — a real
   test-coverage observation about the docstring/test pair — `correct_but_low_value`.

Useful-comment rate (this one PR): 4/4 real observations, 0 false positives,
0 unsupported, 1 of the 4 led to an actual code fix.

## 10–12. Real webhook E2E / synchronize / feedback loop — **NOT RUN** (credit exhaustion)

Infrastructure for this was built and verified working (see §13), but no live
review call could be scheduled for a real PR once credits ran out, so no E2E
was attempted. `LIVE_PROVIDER_CREDIT_EXHAUSTED`.

## 13. Webhook infrastructure (verified functional, not exercised end-to-end)

- A `cloudflared` quick tunnel was started (with explicit user approval, after
  the permission classifier initially and correctly blocked it) and confirmed
  to reach the local API (`/health/live` returned 200 through the tunnel).
- The GitHub App's webhook URL (`PATCH /app/hook/config`, App-JWT auth via
  `patchfrog.github.auth.build_app_jwt`) was updated to the tunnel URL and
  confirmed applied — webhook secret and App permissions/events were never
  touched.
- Once credit exhaustion made further live calls pointless, the webhook URL
  was **reverted to its exact prior value** and the tunnel stopped, restoring
  the App to the state it was in before this session touched it.

## 14. Bugs found and fixed

**1 real bug**, found by a live model during natural-PR dry-run §9, fixed this
session:

- **`patchfrog/publishing/body.py`**: `finding.title` is model-generated
  structured output with no schema constraint against embedded whitespace.
  `.strip()` only trims the ends, and `sanitize_untrusted_text` only redacts
  marker-lookalike sequences — neither collapses an internal `\n`/`\t`. A
  title containing a newline broke the single-line
  `SEVERITY · category — title` header (both the inline-comment header and
  the summary body's per-finding bullet line) across multiple lines.
  Reproduced first, then fixed by collapsing whitespace
  (`" ".join(finding.title.split())`) before interpolation in both call
  sites. Two regression tests added
  (`tests/unit/test_publishing_body.py`). Rerun of the exact reproduction
  after the fix confirms a single-line header.

No other **PatchFrog code defect** was found in any of the ~117 real provider
calls made this session. This is a distinct claim from "the model's output was
always correct" — it was not: §8 documents one unsupported/incorrect
repository-level claim the model made during the context-ablation run, caused
by an incomplete `ContextBundle` rather than a PatchFrog code bug in the usual
sense (nothing crashed, mis-rendered, or leaked; the *content* of one finding
was simply wrong about the fixture it was reviewing).

## 15. Failure safety (no new live calls needed)

Retryable/non-retryable classification and duplicate-publication protection
under provider failure are already covered by existing, passing tests
(`tests/unit/test_ops_errors.py`: `ProviderTransientError` → retryable,
`ProviderFatalError`/`MissingProviderCredentialsError` → never retryable;
`tests/integration/test_publishing_idempotency.py` /
`test_review_pull_request_supersession.py`: no duplicate publication on
retry). Reconfirmed passing as part of this session's gates — no new code
needed here.

## 16. Token / usage profile (real, aggregated across every live call this session)

| | Reviewer in | Reviewer out | Critic in | Critic out |
|---|---|---|---|---|
| Total | 348,625 | 48,748 | 90,481 | 12,285 |

**Grand total: ~500,139 tokens** across 117 successful real provider calls
before the account ran out of credit. See `usage.json` for the per-scenario
breakdown. No dollar cost is estimated (not requested, and pricing/plan tier
isn't visible from this environment).

## 17. Latency (real, observed)

Per-review-run wall-clock time (index+analyze+context+reviewer+critic+persist),
across all completed runs this session: min ~1.0s (trivial/no-candidate runs),
median ~15–20s, worst observed 91.9s (`case11-cross-file`, 12 candidates). See
`latency.json`.

## 18. Readiness classification

# `BLOCKED`

**Primary blocking condition:** live validation could not be completed because
the configured Anthropic account's credit balance was exhausted mid-run. The
`READY_FOR_EXTERNAL_PRIVATE_BETA` bar requires a completed real webhook E2E
and a completed benchmark/security-quality pass, and neither could be
attempted past that point. This is an external funding/billing constraint on
the operator's own account, fully outside PatchFrog's code — re-run this
exact validation once credits are topped up to reach a real readiness verdict.

**Additionally identified product limitation (independent of the credit
issue):** the Context Engine's call-graph expansion is currently 1-hop only
(direct callees, not transitive). The live extern-case check (§8) demonstrated
this can cause a real cross-file contract bug to be missed even when the
underlying symbol-resolution fix (PR #25) is working correctly. **This
limitation is not fixed in PR #30** — it's a real design tradeoff (context
size/noise vs. traversal depth) that deserves its own design discussion, not a
hot-fix bundled into this validation PR.

To be precise about what "every real call succeeded cleanly" does and doesn't
mean here: every call completed technically (no crash, no malformed response,
no leaked secret) and no PatchFrog code defect was found beyond the one fixed
in §14. That is a narrower claim than "every finding was correct" — §8
documents one real unsupported/incorrect finding, caused by incomplete
context, not by anything crashing or by hallucination unconnected to the
supplied input.

## Remaining limitations / what's needed to finish this validation

1. **Anthropic account credit balance** — the primary blocking item for
   *completing* this validation. Everything below (except item 7) is
   otherwise ready to run as soon as credits are available.
2. Full 74-case Phase 8 benchmark + security-quality corpus (live).
3. Critic ON/OFF ablation on a representative subset.
4. Broader context ablation (this session only spot-checked the extern case).
5. Real webhook-driven E2E + synchronize E2E + feedback-loop live check
   (infrastructure proven ready in §13).
6. The 3 remaining natural-PR dry-runs (#25, #28, #29).
7. The Context Engine's 1-hop-only call-graph traversal limit found in §8 is a
   real, documented gap — worth a design discussion in a future session, not a
   hot-fix.

## PR

`chore/live-anthropic-validation` → `main`. **Not merged** — explicit
confirmation below.
