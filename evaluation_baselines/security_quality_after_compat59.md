# PatchFrog Quality Evaluation Report

Generated: 2026-08-22T01:02:44.488961+00:00  
Mode: `full_pipeline`  
Provider/model: `fake-oracle` / `oracle-v1`  
Critic enabled: `True`  
Duration: 695520 ms

## Overall

- Cases: 59 (errors: 0)
- True positives: 39
- False positives: 0
- Missed (false negatives): 0
- Precision: 1.000  Recall: 1.000  F1: 1.000
- False positives / case: 0.000
- False positives / 100 candidates: 0.00

## Safety

- Clean-case pass rate: 1.000 (20/20)
- Average findings on clean cases: 0.000
- Duplicate rate: 0.070
- Unsupported (hallucination) rate: before validation 0.000 (0/36), after validation 0.000 (0/43)
- Severity: exact match 0.974, within one level 1.000, overstated 0.000, understated 0.026 (n=39)

## Category breakdown

| category | support | TP | FP | missed | precision | recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| api_misuse | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| concurrency | 3 | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| correctness | 17 | 17 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| memory_safety | 8 | 8 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| performance | 2 | 2 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| resource_management | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| security | 5 | 5 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| undefined_behavior | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 |

## Difficulty breakdown

| difficulty | support | precision | recall | F1 |
|---|---:|---:|---:|---:|
| easy | 20 | 1.000 | 1.000 | 1.000 |
| medium | 13 | 1.000 | 1.000 | 1.000 |
| hard | 6 | 1.000 | 1.000 | 1.000 |

## Static / AI overlap

- Static-only: 3  AI-only: 33  Both: 3  Missed by both: 0

## Static analyzer coverage

| analyzer | attempted | succeeded | failed | skipped | unsupported | timed out | raw findings |
|---|---:|---:|---:|---:|---:|---:|---:|
| clang_tidy | 22 | 0 | 0 | 0 | 22 | 0 | 0 |
| cppcheck | 22 | 0 | 0 | 0 | 22 | 0 | 0 |
| ruff | 37 | 37 | 0 | 0 | 0 | 0 | 4 |
| semgrep | 59 | 59 | 0 | 0 | 0 | 0 | 5 |

## Efficiency

- Candidates: generated 243, reviewed 243, skipped 0
- Provider calls: 243  (6.23 / true positive)
- Tokens per true positive: input 623.1, output 311.5

## Security review quality

- Scored true positives: 39
- Identification present: 1.000  Root cause present: 1.000
- Actionable fix present: 1.000 (n=0)  Impact grounded: 1.000 (n=0)
- Severity overstatement rate: 0.000 (n=0)  Unsupported-impact rate: 0.000
- Generic-advice rate: 0.000  Low/medium-confidence overclaim rate: 0.000 (n=1)

## STATIC_ONLY (real Phase 3 engine, no LLM)

*STATIC_ONLY: the real Phase 3 static-analysis engine (ruff/semgrep/cppcheck/clang-tidy, whichever are actually installed), no LLM involved at all*

- Static TP: 6  FP: 0  Missed: 0  Precision: 1.000  Recall: 1.000
- Clean-case static false positives: 0 of 20 clean cases (pass rate 1.000)

| analyzer | attempted | succeeded | failed | skipped | unsupported | raw findings |
|---|---:|---:|---:|---:|---:|---:|
| clang_tidy | 22 | 0 | 0 | 0 | 22 | 0 |
| cppcheck | 22 | 0 | 0 | 0 | 22 | 0 |
| ruff | 37 | 37 | 0 | 0 | 0 | 4 |
| semgrep | 59 | 59 | 0 | 0 | 0 | 5 |

## Critic ON vs. OFF

*pipeline/guardrail behavior (oracle-scripted FakeLLMProvider) -- NOT evidence of real critic model quality; only a --provider live run measures that*

| | critic OFF | critic ON | delta |
|---|---:|---:|---:|
| TP | 39 | 39 | 0 |
| FP | 0 | 0 | 0 |
| Missed | 0 | 0 | 0 |
| Precision | 1.000 | 1.000 | +0.000 |
| Recall | 1.000 | 1.000 | +0.000 |
| Unsupported (final) | 0 | 0 | 0 |
| Severity overstatement rate | 0.000 | 0.000 | +0.000 |

## Incremental review memory

- Scenarios passed: 9/9
- Total provider calls avoided: 9
- Unsafe carry-forward count: 0 (target: 0)

  - [OK] `unrelated_change` -- an unrelated file is added; the untouched bug and clean symbol must never be re-reviewed: step2 status=<FindingMemoryStatus.CARRIED_FORWARD: 'carried_forward'> prompted=['greet'] avoided=3
  - [OK] `bug_remains_unchanged` -- a sibling symbol in the same file changes; the untouched bug must still be carried forward: step2 status=<FindingMemoryStatus.CARRIED_FORWARD: 'carried_forward'> prompted=['add', 'math_ops.py']
  - [OK] `bug_fixed` -- the body of the buggy symbol actually changes (fixed) -- must always get a real recheck, never a blind carry-forward: resolved=True prompted=['divide'] statuses_after={}
  - [OK] `evidence_region_changed` -- a change above the bug shifts its line number without changing the bug itself -- must be rechecked fresh, not lost or duplicated: statuses_after={'compute returns wrong boundary': <FindingMemoryStatus.CARRIED_FORWARD: 'carried_forward'>} prompted=['compute']
  - [OK] `symbol_moved` -- the buggy symbol moves to a different file, body unchanged -- the finding must survive the move (carried forward or rechecked), never dropped: statuses_after={'calc_total subtracts instead of adding': <FindingMemoryStatus.CARRIED_FORWARD: 'carried_forward'>} prompted=[]
  - [OK] `file_renamed` -- the file containing the bug is renamed (git mv), content identical -- the finding must survive: statuses_after={'legacy_check inverted': <FindingMemoryStatus.CARRIED_FORWARD: 'carried_forward'>} prompted=[]
  - [OK] `function_renamed_ambiguously` -- a rename introduces a second equally-plausible symbol of the same name -- must never silently resolve or carry forward without a fresh look: statuses_after={'handler off by one': <FindingMemoryStatus.OPEN: 'open'>} prompted=['process'] resolved_titles=frozenset({'handler off by one'})
  - [OK] `force_push` -- history is force-pushed/rewritten -- incremental reuse must never be trusted across a non-ancestor rewrite: mode=<IncrementalRunMode.FULL: 'full'> ancestry_verified=False avoided=0
  - [OK] `base_change` -- the PR's declared base moves forward independently of HEAD's own commit chain -- must not spuriously break incremental reuse: mode=<IncrementalRunMode.INCREMENTAL: 'incremental'> ancestry_verified=True statuses_after={'divide subtracts instead of dividing': <FindingMemoryStatus.CARRIED_FORWARD: 'carried_forward'>}
