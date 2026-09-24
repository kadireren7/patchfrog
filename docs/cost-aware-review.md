# Cost-Aware Review (M4 — Ultra-Low-Cost Review Engine)

PatchFrog's product direction is dependency/API compatibility
(see `docs/architecture.md`). Generic PR review is now a **secondary
safety/verification capability** — it has to be cheap enough to run
inside future dependency-migration workflows. M4 makes it so without
suppressing findings.

**PatchFrog decides how much model work is justified — before any
provider call, from deterministic evidence, never from an LLM router.**

## Execution shape

```
diff ──► change/risk classifier (patchfrog.change_risk, pure, no LLM)
           │
           ├─ NO_AI ─────────► persisted SUCCEEDED run, 0 provider calls,
           │                   visible check: "deterministic review, no AI calls: <reason>"
           │
           └─ TINY/NORMAL/ELEVATED/HIGH_RISK
                 │  per-tier budget on the one ReviewBudget ledger
                 ▼
           prepare candidates (context, redaction, evidence, effort decision) — no calls
                 ▼
           ONE single-pass UNIFIED call per batch of candidates (shared, de-duplicated context)
                 ▼
           sequential escalation (ELEVATED/HIGH_RISK only, each with a typed reason)
                 ▼
           unchanged post-proposal pipeline per candidate:
           validation → cross-role grouping → on-demand critic → contradiction resolution
                 ▼
           confidence aggregation → dedup → persistence → publication
```

Selected with the operator setting `PATCHFROG_REVIEW_STRATEGY`
(`cost_aware` default, or `specialist_fanout` for the pre-M4
per-candidate Correctness+Security fan-out, kept intact).

## Risk tiers (`patchfrog/change_risk/`)

| Tier | When | Default total call ceiling | of which review-phase |
|---|---|---|---|
| `no_ai` | only docs, comments/blank lines, lockfiles, or policy-excluded generated/vendored files | 0 | 0 |
| `tiny` | ≤ 20 semantic lines in ≤ 2 files (test-only: ≤ 80), no elevating signal | 2 | **1** |
| `normal` | everything else without an elevating signal | 2 | 1 |
| `elevated` | any one elevating signal | 3 | 2 |
| `high_risk` | corroborated high-risk combination (below) | 5 | 3 |

Elevating signals: large change, deletion-heavy, cross-module (≥ 3
modules), dependency manifest, CI/workflow config, public interface
removed/modified, schema/model, database migration, security-sensitive
path, static HIGH/security finding on changed code.

`high_risk` requires corroboration: a security-sensitive path or static
HIGH/security finding **plus** another elevating signal; or a migration
plus schema/public-interface/large change; or three elevating signals. A
security path token alone is only `elevated` — a path keyword is never
the sole decisive signal.

Every classification carries typed `signals` and human `reasons`
(counts/paths only, never line content) and is persisted on the run.

### Comment detection is conservative

- `#` is a comment only in hash-comment languages; in C/C++ it is the
  preprocessor. Shebang, encoding, `# type:`, `//go:`, `// +build`,
  `// @ts-`, `/// <reference` are code.
- C `*` continuation lines count only inside a `/* … */` the hunk itself
  opened (`*ptr = 1;` is code).
- Python `#` lines are verified **exactly** with the standard-library
  tokenizer against the head (and, when known, base) file content — read
  with the existing bounded, never-executing single-commit reader, only
  for files whose changed lines all look like comments. Without content,
  a diff-only triple-quote parity rule applies.
- Binary/too-large files are never `no_ai` or `tiny`.

## Budgets (`patchfrog/review/cost_policy.py`, `patchfrog/review/budget.py`)

`effective max_provider_calls = min(repository max_provider_calls,
tier total)`. Reviewer calls, escalation calls, critic calls, retries
and one-hop fallbacks all count against the same `ReviewBudget` ledger.
Inside the total, review-phase calls are capped at `total − verification
reserve`; only critic calls may use the reserve, so review work can
never starve a mandatory verification.

**Why TINY's total is 2, not 1.** With a total of 1, a tiny PR whose
single pass finds a HIGH/security bug could never be critic-verified and
the existing safety rule would suppress it. That is cost saved by
dropping a useful finding, which M4 rules out. A clean tiny PR still
costs exactly **one** call; the second is spent only when a finding needs
verification. Operators who want a strict single call can set
`PATCHFROG_RISK_TIER_MAX_PROVIDER_CALLS={"tiny":1}` and accept that such
findings are suppressed (never published unverified).

Exhaustion ends the run `PARTIAL` with a typed reason
(`budget_status`), never as a silent clean review.

## Single pass and escalation (`patchfrog/review/single_pass.py`)

- One `review_response:unified` call covers a batch of candidates
  (correctness, security, contracts/interfaces, regression risk, test
  implications). Batches are greedy and order-preserving up to
  `single_pass_max_input_tokens` (24k estimated); repository context
  blocks are shown once per batch.
- Returned findings are validated against exactly the text that call was
  shown, then attributed deterministically to one candidate (overlap in
  the same file → nearest in the same file → the candidate shown that
  file as context).
- Escalation is sequential, only for `elevated`/`high_risk`, and only
  while a critic call would still remain. Reasons:
  `security_sensitive_change`, `static_high_risk_finding`,
  `ci_config_change`, `high_risk_first_pass_finding` (Security specialist);
  `database_migration`, `public_contract_change`,
  `cross_module_complexity` (Correctness specialist, `high_risk` only, and
  only for candidates the Security escalation does not already cover).

## Critic (unchanged policy, now on demand by construction)

No valid finding → zero critic calls. `CriticSelectionPolicy` +
`CriticExpectation` are unchanged: a low-risk, high-confidence finding on
a LIGHT candidate may skip the critic; HIGH/CRITICAL severity or
security category always gets it. `CriticFailurePolicy` is unchanged.

## Context minimization

TINY/NORMAL runs scale each candidate's context budget by 0.35/0.5 and
disable adaptive (depth-2) expansion — broader context needs a reason
those tiers do not have. ELEVATED/HIGH_RISK keep the existing per-
candidate policy. Runs record `context_initial_tokens`,
`context_expanded_tokens` and `context_expansion_reasons` (estimates and
reason codes, never text).

## Exact-head reuse and force

A `SUCCEEDED` run is reused for the identical `(repository, head SHA,
review config, model identity incl. cost-policy fingerprint, incremental
mode)` with zero provider calls; publication/check reconciliation still
runs. A different head, policy or mode never reuses. `--force` (CLI) /
`force_review=True` gives the run a one-off salted identity and records
`forced=true`.

## Observability

`ReviewRunSummary.cost_report()`, the CLI review output, the persisted
`review_runs` columns (migration `0033_review_cost_engine`) and the
telemetry snapshot's `cost` section (schema v12) answer: risk tier and
signals, provider/reviewer/critic calls, retries, provider/model,
estimated input/output tokens and cost, escalation reasons, cache hit,
context cost, budget status. Prompts, source, responses, keys and secret
values are never recorded.

## Benchmark

```bash
python -m patchfrog.cli eval cost-benchmark          # Markdown
python -m patchfrog.cli eval cost-benchmark --json   # JSON
python -m patchfrog.cli eval run --beta-readiness --review-strategy specialist_fanout  # "before" arm
```

Ten deterministic scenarios (`tests/fixtures/cost_benchmark/`), a
scripted fake reviewer, deterministic token estimates of the exact
prompts, synthetic prices. It measures PatchFrog's call shape and cost,
**never model quality**. The committed result is
`evaluation_baselines/m4_cost_benchmark.{json,md}`.
