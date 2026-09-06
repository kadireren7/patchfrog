# Review Effectiveness Benchmark Foundation -- Summary (Milestone S, Part F)

## 1. Problem statement

`patchfrog/evaluation`'s existing FakeLLM/oracle corpus
(`patchfrog/evaluation/fixtures.py`) proves the review *engine* is
deterministic: given the same candidate/context/policy inputs, the same
finding comes out, every time, with no orchestration bug silently
dropping or duplicating a finding. It does **not** answer a different
question that matters just as much: is a given finding actually a real,
useful bug report? Milestone S's own product principle -- "find fewer,
harder, evidence-backed problems" -- has no way to be measured without a
corpus of real scenarios with known, human-authored ground truth. Part F
builds the narrowest safe foundation for that corpus.

## 2. Schema (`patchfrog/evaluation/review_effectiveness.py`)

- `ScenarioCategory` -- what *kind of scenario* a case represents
  (distinct from `FindingCategory`, which describes what kind of bug a
  finding is). Thirteen values spanning clean-PR positive controls
  through Milestone S's own `executable_verification_case`.
- `ReviewEffectivenessGroundTruth` -- explicit, hand-authored expected
  truth: `expected_clean` (positive control), `must_find_file_path` /
  `must_find_qualified_name` / `expected_finding_category` /
  `acceptable_severity_min` / `acceptable_severity_max` (must-find
  obligation for a real bug), `must_not_find_file_path` /
  `must_not_find_qualified_name` (explicit false-positive guard for a
  symbol that must NOT be flagged), and `expected_execution_outcome`
  (a plain string, for `executable_verification_case` only -- kept
  decoupled from `patchfrog.executable_verification.domain`'s own enum
  so this schema never needs Milestone S's package as a hard
  import-time dependency).
- `ReviewEffectivenessCase` -- `case_id` + `category` + `description` +
  `ground_truth`, loaded from one JSON file each.
- `load_case` / `load_all_cases` -- pure JSON loaders; `load_all_cases`
  rejects a duplicate `case_id` across the corpus with a `ValueError`
  rather than silently overwriting one case with another.
- `ActualCaseOutcome` -- what actually happened for one case during one
  benchmark run, supplied by the caller. This module has no
  provider/orchestration dependency of its own; it stays a pure
  schema/metrics layer, matching the same separation-of-concerns
  already used by `patchfrog/evaluation/fixtures.py`.
- `compute_metrics` -- bounded, explainable metrics only (spec Part F3:
  never an invented/unsupported one): `case_recall` (of non-clean cases
  with a recorded outcome, how many matched their must-find
  obligation), `clean_case_precision` (of clean cases with a recorded
  outcome, how many produced zero findings), plus raw counts for false
  positives, duplicates, and must-not-find violations. A case with no
  recorded outcome is excluded from the count it would have
  contributed to -- never defaulted to pass or fail.

## 3. Corpus (`validation/review_effectiveness/corpus/`)

Three example cases populate the foundation, each proving one distinct
path through the schema:

- `clean_pr_001` (`clean_pr`) -- a docstring-only change; the positive
  control for clean-case precision. Expects zero findings.
- `local_correctness_bug_001` (`local_correctness_bug`) -- a real,
  self-contained off-by-one bug in `src/pagination.py::paginate`.
  Expects a `correctness` finding at that exact symbol, medium-to-high
  severity.
- `executable_verification_case_001` (`executable_verification_case`)
  -- a changed function with a real, existing, discoverable pytest test
  that genuinely fails on the new head. Expects
  `expected_execution_outcome: "confirmed_failure"`, proving Milestone
  S's own `EXISTING_TARGETED_TEST` verification path is exercised by
  this benchmark, not just by `test_executable_verification_corpus.py`.

The remaining ten `ScenarioCategory` values are defined but
deliberately left unpopulated in this milestone (see README's "narrow
foundation" note).

## 4. FakeLLM limitation (restated, binding)

Every case in this corpus is meant to be run against FakeLLM/oracle,
exactly like `patchfrog/evaluation`'s own existing corpus. **FakeLLM
proves deterministic orchestration and evaluation correctness. It does
not prove real-model review quality.** No claim of real production
precision/recall is made from this corpus alone.

## 5. No live provider calls (restated, binding)

This benchmark never calls Anthropic or OpenAI, and never calls Gemini
absent an explicit, zero-cost, automated reason to. No such reason
exists for the three cases populated in this milestone, so zero live
provider calls were made while building or testing this foundation.

## 6. Tests

`tests/unit/test_review_effectiveness.py` (14 tests, all passing):
loader correctness for a full ground-truth block, a clean case, a
missing-ground-truth-block default, and an execution-outcome case;
`load_all_cases` returns `()` for a missing directory and raises
`ValueError` on a duplicate `case_id`; `load_all_cases` against the
real `DEFAULT_CORPUS_ROOT` finds all three example cases;
`compute_metrics` correctness for clean-case precision (both zero and
nonzero false positives), non-clean-case recall (both matched and
missed), a case with no recorded outcome (excluded, never defaulted),
duplicate/violation counts, and a mixed corpus with partial recall and
full precision.

## 7. Gates

`ruff check` and `mypy` both pass clean on
`patchfrog/evaluation/review_effectiveness.py` and
`tests/unit/test_review_effectiveness.py`.

## 8. Scope decision

This is Milestone S's S4 sub-phase: benchmark **foundation** only --
schema, loader, metrics, and a handful of example cases proving the
foundation is correct end-to-end. Populating the remaining nine
scenario categories, wiring an actual FakeLLM benchmark-runner harness
that consumes this schema end-to-end against the real orchestrator, and
any real-provider run are explicitly deferred to a future milestone.
