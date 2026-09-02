# Change Intelligence Foundation Validation

Record of the audit, the resulting architecture/code/docs, and the
controlled-corpus validation performed for Milestone J ("Change
Intelligence Foundation"). See `latest-summary.md` for the full
narrative — audit findings, design decisions, corpus results, and gate
results.

**Zero additional provider calls were added by this milestone.** Every
module in `patchfrog/change_intelligence/` is pure/deterministic
(structurally enforced by
`tests/unit/test_change_intelligence_versioning.py::test_change_intelligence_never_calls_a_provider`
— no `LLMProvider` import anywhere in the package). No live Anthropic
call, no live Gemini call, and no Cloud/dashboard work were required or
performed for this milestone.

Ground truth for the controlled corpus
(`tests/integration/test_change_intelligence_corpus.py`) is entirely
synthetic, purpose-built git fixture repositories — never real customer
or production repository content.

No credentials, installation tokens, webhook secrets, or raw private
source content appear anywhere in this directory.
