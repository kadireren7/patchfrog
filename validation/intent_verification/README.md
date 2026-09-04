# Intent Verification Foundation Validation

Record of the audit, the resulting architecture/code/docs, and the
controlled-corpus validation performed for Milestone L ("Intent
Verification Foundation"). See `latest-summary.md` for the full
narrative — audit findings, design decisions, corpus results, and gate
results.

**Zero additional provider calls were added by this milestone.** Every
module in `patchfrog/intent_verification/` is pure/deterministic
(structurally enforced by
`tests/integration/test_intent_verification_corpus.py::test_intent_verification_never_calls_a_provider`
— no `LLMProvider` import anywhere in the package). No live Anthropic
call, no live Gemini call, no OpenAI call, and no Cloud/dashboard work
were required or performed for this milestone.

Ground truth for the controlled corpus
(`tests/integration/test_intent_verification_corpus.py`,
`tests/integration/test_intent_verification_review_pipeline.py`) is
entirely synthetic, purpose-built git fixture repositories with
synthetic PR title/body text — never a real customer PR description or
production repository content.

No credentials, installation tokens, webhook secrets, or raw private
source/PR content appear anywhere in this directory.
