# Repository Learnings Foundation Validation

Record of the audit, the resulting architecture/code/docs, and the
controlled-corpus validation performed for Milestone O ("Repository
Learnings Foundation"). See `latest-summary.md` for the full narrative
-- audit findings, design decisions, corpus results, and gate results.

**Zero additional provider calls were added by this milestone.** Every
module in `patchfrog/repository_learnings/` is deterministic
(structurally enforced by
`tests/integration/test_repository_learnings_corpus.py::test_repository_learnings_never_imports_a_provider`
-- no `LLMProvider` import anywhere in the package). No live Anthropic
call, no live Gemini call, no OpenAI call, and no Cloud/dashboard work
were required or performed for this milestone.

**Zero new SQL queries were added.** This milestone consumes Milestone
N's own already-fetched `HistoricalRegressionRecord`s
(`HistoricalRegressionReport.trusted_records_considered`) directly --
there is no second trust query, no second temporal model, and
`patchfrog/repository_learnings/` has no `queries.py` module at all.

**No new table was added** -- only five nullable-default summary
columns on `review_runs` (migration `0023_repository_learnings`), the
same pattern every prior Intelligence milestone established.
`RepositoryLearning` itself is never persisted as its own row --
always re-derived live, per review run, from data N's own query
already reads.

Ground truth for the controlled corpus
(`tests/integration/test_repository_learnings_corpus.py`) is entirely
synthetic, purpose-built git fixture repositories and directly
-persisted (never FakeLLM-authored) historical review/feedback state --
never a real customer PR description, production repository content,
or a hand-constructed record standing in for a real database round
trip.

No credentials, installation tokens, webhook secrets, or raw private
source/PR content appear anywhere in this directory.
