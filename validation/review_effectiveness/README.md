# Review Effectiveness Benchmark Foundation

Record of the foundation laid for Milestone S, Part F ("Review
Effectiveness Benchmark"). See `latest-summary.md` for the full
narrative -- schema design, corpus, and the FakeLLM limitation.

**This benchmark measures a different claim than
`patchfrog/evaluation`'s existing corpus.** The existing corpus proves
the review *engine* is deterministic and correct (same input, same
output; no orchestration bugs). This benchmark proves (or disproves) a
separate claim: whether PatchFrog's findings are actually *useful* --
did it find the real bug, at the right symbol, without drowning it in
noise on a clean change.

**FakeLLM limitation (explicit, non-negotiable):** every case here is
run against FakeLLM/oracle, exactly like the existing evaluation
harness. **FakeLLM proves deterministic orchestration and evaluation
correctness. It does not prove real-model review quality.** No claim of
real production precision/recall is made from this corpus alone, now
or in any future milestone that extends it, until a real-provider run
is explicitly commissioned and documented as such.

**No live provider calls.** This benchmark never calls Anthropic or
OpenAI, and never calls Gemini absent an explicit, zero-cost, automated
reason to. None of the current corpus cases required one.

**Deliberately narrow foundation, not a complete corpus.** Milestone S
populates a handful of example cases (`clean_pr`,
`local_correctness_bug`, `executable_verification_case`) to prove the
schema, loader, and metrics are correct end-to-end. The remaining
`ScenarioCategory` values (`cross_file_correctness_bug`,
`contract_break`, `stale_consumer`, `intent_miss`,
`missing_test_evidence`, `test_weakening`, `security_issue`,
`historical_regression`, `noisy_negative`, `duplicate_finding`) are
defined in the schema but intentionally left unpopulated -- adding a
case is just adding a new JSON file under `corpus/`, so the corpus can
grow incrementally without any code change.

**Ground truth is hand-authored, never LLM-generated.** Each case's
`ground_truth` block is written by a human against a real, known change
-- what file/symbol must be found, what category/severity is
acceptable, or that the change is a positive control expected to
produce zero findings.

No credentials, installation tokens, webhook secrets, or raw private
source/PR content appear anywhere in this directory.
