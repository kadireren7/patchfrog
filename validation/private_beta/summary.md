# Private Beta Validation Sprint -- Summary

Branch `chore/private-beta-validation`, baseline `main` @ `f7e4735e464e4c09752894a85a19c66456f2a8dc`.

No live `ANTHROPIC_API_KEY` exists in this environment (unchanged since
Phase 5). Every AI-review call in this sprint used a scripted
`FakeLLMProvider` -- never claimed as real AI quality. Two disjoint
labels are used throughout:

- **`pipeline_correctness`** (controlled cases): the reviewer is
  scripted from human-authored ground truth. Proves the full real
  pipeline -- diff -> index -> static analysis -> context -> candidate
  generation -> validation -> critic -> persistence -- correctly
  carries a *known* finding through end to end. Says nothing about
  whether a real model would have found the bug.
- **`natural_static_only`** (natural PRs): the reviewer is a hard no-op
  (always returns zero findings). Only real static-analyzer findings
  (Ruff/Semgrep, real tools, real output) are evaluated.

## Repositories (5, spec section 3)

| Repo | Language(s) | Files | Why chosen |
|---|---|---|---|
| `kadireren7/patchfrog` (real, real App-installed) | Python | 601 tracked | Python service/application; real GitHub App already installed, only repo used for the real end-to-end dogfood |
| `redis/hiredis` (real clone) | C | 79 tracked, 17.6k LOC | C project |
| `Tencent/rapidjson` (real clone) | C++ | 314 tracked, 39.9k LOC | C++ project |
| `ultrajson/ultrajson` (real clone) | C + Python | 103 tracked (12 own-code excl. vendored `deps/`) | mixed-language repo |
| self-authored `clean_fixture` (4 files) | Python | 4 | deliberately clean/low-noise repo -- a real OSS repo can't guarantee zero known issues, this one can |

## PR scenarios (21 review events, spec section 4)

All 15 required categories covered: obvious bug, subtle bug, security
bug, memory/resource bug, concurrency bug, API misuse, clean refactor,
doc-only, formatting-only, multi-file bug, cross-file dependency, large
PR near limits, PR synced twice (duplicate webhook, real dogfood), bug
persists across 2nd commit, bug fixed on next commit. See
`pr_results.jsonl` for the full per-PR table.

## Controlled results (pipeline_correctness)

- 8 single-shot planted-bug cases: **TP=8, FP=0, missed=0** (precision/recall/F1 = 1.0)
- 4 clean/refactor/doc/formatting cases: **0/4 false positives**
- 3-commit incremental arc (case12): bug correctly **carried forward**
  across an unrelated commit (0 extra provider calls spent on it) and
  correctly **resolved** when fixed on commit 3 -- `unsafe_carry_forward=0`,
  `duplicate_publication=0`

## Natural results (natural_static_only)

6 real PRs (1 large 27-file feature PR, 2 real historical bug-fix
commits, 3 real trivial/doc/test-only commits) across patchfrog/hiredis/
rapidjson/ultrajson. Raw static-analysis counts are **repo-wide**
snapshots (33 total), not diff-scoped -- verified by direct DB query
that **0 of these findings actually fall within any of the 5 small
natural PRs' changed files** (n1's 27 files not individually
re-verified). Real natural-PR noise in this sample: **0/5 relevant**.

## Real GitHub dogfood (kadireren7/patchfrog#23, closed + branch deleted)

1. **Onboarding without manual DB edit**: real `installation` webhook
   (`created`, installation 153810631) automatically created the
   `installations` row -- verified empty before, populated after,
   zero manual SQL.
2. **Automatic orchestration**: a real `pull_request` webhook
   automatically chained ingest -> eligibility -> orchestrator ->
   index -> analyze -> review, with real GitHub API calls throughout
   (installation tokens, PR metadata, changed files) -- the headline
   new capability of Public Beta Readiness, proven live for the first
   time.
3. **Credential-boundary failure handled cleanly**: review correctly
   failed at `MissingProviderCredentialsError` -- no stuck run, no
   partial publish, `reviews_started_total`/`reviews_failed_total`
   incremented. **A real classification bug was found and fixed** (see
   `failures.json`), reverified live post-fix.
4. **Duplicate webhook idempotency**: redelivering the identical PR
   webhook reused the index (incremental) and reused the analysis run
   rather than recomputing -- no double work, no double DB rows.
5. **Safe publication default**: a real `--publish` attempt against
   `kadireren7/patchfrog` (no `.patchfrog.yml`) correctly wrote **zero**
   GitHub comments (`PublicationConfig().enabled` defaults `False`).

## Context engine finding (not fixed, documented limitation)

For case11 (cross-file dependency), the context bundle built for
`conn_manager.c`'s target symbol pulled in `hiredis.h` (via `#include`)
but **not** `retry_policy.h` -- the file containing the exact
`RETRY_POLICY_MAX_ATTEMPTS` contract the bug violates (the caller only
has an `extern` declaration + a separately-compiled implementation
file, not a `#include`). The scripted oracle still matched (it's
symbol-name-keyed, not context-dependent), so this did **not** show up
as a missed finding in the pipeline-correctness numbers above -- but a
real model given only what PatchFrog actually assembled would plausibly
have missed this bug. Not fixed this sprint (C `extern`-to-definition
symbol resolution is Phase 2/4 architecture, out of "smallest fix"
scope) -- recorded as a remaining limitation.

## Critic ON/OFF

Re-ran 3 controlled cases with critic enabled. No delta observed
(expected: the oracle's critic response is scripted to always accept,
so this only proves critic invocation/persistence plumbing survives
end-to-end, not real FP-reduction value -- Phase 8's own benchmark
baseline is the source of truth for real critic value).

## Latency (cold, single-process, real repos -- see `latency.json`)

n=21, median 10.9s, p90 14.5s, worst 16.9s (the 27-file natural PR).
This is first-index-no-reuse CLI-driven latency, not production
warm-worker steady-state.

## Bugs found and fixed: 1

See `failures.json` for full detail. `patchfrog/ops/errors.py`'s
`classify_exception()` now correctly classifies
`MissingProviderCredentialsError` as `PROVIDER_ERROR` (non-retryable)
instead of the generic `INTERNAL_ERROR` catch-all. Regression test:
`tests/unit/test_ops_errors.py::test_missing_provider_credentials_is_provider_error_never_retryable`.
Reverified against real live dogfood data post-fix.

## Readiness classification

**READY_WITH_LIMITATIONS** -- see the PR description / final report for
full reasoning and the complete remaining-limitations list.
