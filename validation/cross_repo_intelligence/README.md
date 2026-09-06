# Cross-Repo Intelligence Foundation Validation

Record of the audit, the resulting architecture/code/docs, and the
controlled-corpus validation performed for Milestone R ("Cross-Repo
Intelligence Foundation"). See `latest-summary.md` for the full
narrative -- audit findings (including the confirmed
`.patchfrog.yml`-is-read-from-the-PR's-own-head security finding),
design decisions, corpus results, and gate results.

**Zero additional unconditional provider calls were added by this
milestone.** Every module in `patchfrog/cross_repo_intelligence/` is
deterministic (structurally enforced by
`tests/integration/test_cross_repo_intelligence_corpus.py::test_cross_repo_intelligence_never_imports_a_provider`
-- no `LLMProvider` import anywhere in the package). No live Anthropic
call, no live Gemini call, no OpenAI call, and no Cloud/dashboard work
were required or performed for this milestone.

**Zero cross-repository crawler, zero automatic discovery, zero
package-manifest/submodule parsing.** This milestone reuses Contract &
Blast Radius Intelligence's own already-computed
`ContractIntelligenceReport.deltas` directly and reads only two new,
operator-registered tables -- see `latest-summary.md` sections 1-13.

**A PR under review can never expand PatchFrog's repository access
scope** -- structurally enforced
(`test_cross_repo_intelligence_never_reads_repository_config`,
`test_case_build_report_signature_accepts_no_relation_data`) and
confirmed by direct audit of `patchfrog/review/config_resolution.py`
(`.patchfrog.yml` is read at exactly the PR's own head commit).
Registration is reachable only through the trusted-operator-only
`python -m patchfrog.cli cross-repo` CLI, manually verified end-to-end
against a real Postgres database during implementation (add/list/
remove for both contract keys and relations, plus the self-relation
and unknown-repository error paths).

**No new table duplicates existing state** -- `repository_contract_keys`/
`repository_relations` are the smallest addition needed (see
`latest-summary.md` section 9's finding that Contract Intelligence's
own `ContractDelta` is never persisted and has no cross-repository
-meaningful identity on its own). Six nullable-default summary *count*
columns on `review_runs` (migration `0027_cross_repo_intelligence`) --
no repository name/id, no contract key string, no organization name in
telemetry.

Ground truth for the controlled corpus
(`tests/integration/test_cross_repo_intelligence_corpus.py`) is
entirely synthetic, purpose-built `repositories`/
`repository_contract_keys`/`repository_relations` rows staged directly
via the real repositories (`RepositoryRepository`,
`RepositoryContractKeyRepository`, `RepositoryRelationRepository`) --
never a hand-constructed `CrossRepoPeer` standing in for a real
database round trip.

No credentials, installation tokens, webhook secrets, or raw private
source/PR content appear anywhere in this directory.
