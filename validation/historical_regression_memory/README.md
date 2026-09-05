# Historical Regression Memory Foundation Validation

Record of the audit, the resulting architecture/code/docs, and the
controlled-corpus validation performed for Milestone N ("Historical
Regression Memory Foundation"). See `latest-summary.md` for the full
narrative -- audit findings, design decisions, corpus results, and gate
results.

**Zero additional provider calls were added by this milestone.** Every
module in `patchfrog/historical_regression_memory/` is deterministic
(structurally enforced by
`tests/integration/test_historical_regression_memory_corpus.py::test_historical_regression_memory_never_imports_a_provider`
-- no `LLMProvider` import anywhere in the package). No live Anthropic
call, no live Gemini call, no OpenAI call, and no Cloud/dashboard work
were required or performed for this milestone.

**No new history database was added** -- the one bounded SQL query this
milestone issues reuses Phase 9's own `feedback_assessments` table
joined with the existing `ai_findings`/`review_candidates`/`review_runs`
chain. Only five nullable-default summary columns were added to
`review_runs` (migration `0022_historical_regression`), the same
pattern every prior Intelligence milestone established.

Ground truth for the controlled corpus
(`tests/integration/test_historical_regression_memory_corpus.py`) is
entirely synthetic, purpose-built git fixture repositories and directly
-persisted (never FakeLLM-authored) historical review/feedback state --
never a real customer PR description, production repository content,
or a hand-constructed record standing in for a real database round
trip.

No credentials, installation tokens, webhook secrets, or raw private
source/PR content appear anywhere in this directory.
