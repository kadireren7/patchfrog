# Trajectory Intelligence Foundation Validation

Record of the audit, the resulting architecture/code/docs, and the
controlled-corpus validation performed for Milestone P ("Trajectory
Intelligence Foundation"). See `latest-summary.md` for the full
narrative -- audit findings, design decisions, corpus results, and gate
results.

**Zero additional unconditional provider calls were added by this
milestone.** Every module in `patchfrog/trajectory_intelligence/` is
deterministic (structurally enforced by
`tests/integration/test_trajectory_intelligence_corpus.py::test_trajectory_intelligence_never_imports_a_provider`
-- no `LLMProvider` import anywhere in the package). No live Anthropic
call, no live Gemini call, no OpenAI call, and no Cloud/dashboard work
were required or performed for this milestone. This package's one
orchestration effect (`ReviewEffortPolicy.decide_provisional` gaining
a `trajectory_signal_present` structural signal) can only ever change
*how* an already-real candidate is reviewed (tier, critic strictness,
dispatch order) -- it structurally cannot trigger a provider call for a
candidate that would not otherwise have been reviewed at all.

**Zero new git operations, zero new GitHub API calls, zero new history
crawler.** This milestone reuses Phase 7's own already-persisted,
already-ancestry-verified `review_generations` lineage and Phase 5's
own already-persisted `review_candidates` rows directly -- see
`latest-summary.md` sections 1-5, 11.

**No new table was added** -- only six nullable-default summary *count*
columns on `review_runs` (migration `0024_trajectory_intelligence`).
Trajectory signals are never persisted as their own row; every report
is re-derived live, per review run, from `review_generations`/
`review_candidates`.

Ground truth for the controlled corpus
(`tests/integration/test_trajectory_intelligence_corpus.py`) is
entirely synthetic, purpose-built `review_generations`/
`review_candidates`/`review_runs`/`pull_requests` rows staged directly
via the real repositories (`ReviewGenerationRepository`,
`ReviewCandidateModel`) -- never a real customer PR description,
production repository content, or a hand-constructed `TrajectoryHead`
standing in for a real database round trip.

No credentials, installation tokens, webhook secrets, or raw private
source/PR content appear anywhere in this directory.
