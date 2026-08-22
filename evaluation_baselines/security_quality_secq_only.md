# PatchFrog Quality Evaluation Report

Generated: 2026-08-22T01:07:35.183982+00:00  
Mode: `full_pipeline`  
Provider/model: `fake-oracle` / `oracle-v1`  
Critic enabled: `True`  
Duration: 60023 ms

## Overall

- Cases: 15 (errors: 0)
- True positives: 13
- False positives: 0
- Missed (false negatives): 0
- Precision: 1.000  Recall: 1.000  F1: 1.000
- False positives / case: 0.000
- False positives / 100 candidates: 0.00

## Safety

- Clean-case pass rate: 1.000 (2/2)
- Average findings on clean cases: 0.000
- Duplicate rate: 0.059
- Unsupported (hallucination) rate: before validation 0.000 (0/13), after validation 0.000 (0/17)
- Severity: exact match 0.923, within one level 1.000, overstated 0.000, understated 0.077 (n=13)

## Category breakdown

| category | support | TP | FP | missed | precision | recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| concurrency | 1 | 1 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| maintainability | 0 | 0 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| memory_safety | 3 | 3 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| security | 9 | 9 | 0 | 0 | 1.000 | 1.000 | 1.000 |

## Difficulty breakdown

| difficulty | support | precision | recall | F1 |
|---|---:|---:|---:|---:|
| easy | 2 | 1.000 | 1.000 | 1.000 |
| medium | 8 | 1.000 | 1.000 | 1.000 |
| hard | 3 | 1.000 | 1.000 | 1.000 |

## Static / AI overlap

- Static-only: 0  AI-only: 12  Both: 1  Missed by both: 0

## Static analyzer coverage

| analyzer | attempted | succeeded | failed | skipped | unsupported | timed out | raw findings |
|---|---:|---:|---:|---:|---:|---:|---:|
| clang_tidy | 3 | 0 | 0 | 0 | 3 | 0 | 0 |
| cppcheck | 3 | 0 | 0 | 0 | 3 | 0 | 0 |
| ruff | 12 | 12 | 0 | 0 | 0 | 0 | 1 |
| semgrep | 15 | 15 | 0 | 0 | 0 | 0 | 3 |

## Efficiency

- Candidates: generated 55, reviewed 55, skipped 0
- Provider calls: 55  (4.23 / true positive)
- Tokens per true positive: input 423.1, output 211.5

## Security review quality

- Scored true positives: 13
- Identification present: 1.000  Root cause present: 1.000
- Actionable fix present: 1.000 (n=13)  Impact grounded: 0.923 (n=13)
- Severity overstatement rate: 0.000 (n=13)  Unsupported-impact rate: 0.000
- Generic-advice rate: 0.000  Low/medium-confidence overclaim rate: 0.000 (n=0)
