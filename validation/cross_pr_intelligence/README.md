# Cross-PR Intelligence Foundation Validation

Record of the audit, the resulting architecture/code/docs, and the
controlled-corpus validation performed for Milestone Q ("Cross-PR
Intelligence Foundation"). See `latest-summary.md` for the full
narrative -- audit findings (including the necessary
`PullRequestModel.state` precursor fix), design decisions, corpus
results, and gate results.

**Zero additional unconditional provider calls were added by this
milestone.** Every module in `patchfrog/cross_pr_intelligence/` is
deterministic (structurally enforced by
`tests/integration/test_cross_pr_intelligence_corpus.py::test_cross_pr_intelligence_never_imports_a_provider`
-- no `LLMProvider` import anywhere in the package). No live Anthropic
call, no live Gemini call, no OpenAI call, and no Cloud/dashboard work
were required or performed for this milestone. This package's one
orchestration effect (`ReviewEffortPolicy.decide_provisional` gaining
a `cross_pr_signal_present` structural signal) can only ever change
*how* an already-real candidate is reviewed (tier, critic strictness,
dispatch order) -- it structurally cannot trigger a provider call for a
candidate that would not otherwise have been reviewed at all.

**Zero new GitHub API calls, zero polling loop, zero cross-repository
comparison.** This milestone reuses already-ingested webhook state
(`pull_requests`) and Phase 5's/Phase 7's own already-persisted
`review_generations`/`review_candidates` rows directly -- see
`latest-summary.md` sections 1-6, 13. The one precursor fix required
(closed/merged webhook handling) also adds zero new GitHub API calls --
it is strictly cheaper than the existing `opened`/`reopened`/
`synchronize` ingestion path, since everything needed is already on the
verified webhook payload.

**No new table was added** -- only six nullable-default summary *count*
columns on `review_runs` (migration `0025_cross_pr_intelligence`).
Cross-PR signals are never persisted as their own row; every report is
re-derived live, per review run, from `pull_requests`/
`review_generations`/`review_candidates`.

Ground truth for the controlled corpus
(`tests/integration/test_cross_pr_intelligence_corpus.py`) is entirely
synthetic, purpose-built `pull_requests`/`review_generations`/
`review_candidates`/`review_runs` rows staged directly via the real
repositories (`PullRequestRepository`, `ReviewGenerationRepository`) --
never a hand-constructed `CrossPRPeer` standing in for a real database
round trip.

No credentials, installation tokens, webhook secrets, or raw private
source/PR content appear anywhere in this directory.
