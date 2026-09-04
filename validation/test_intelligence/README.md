# Test Intelligence Foundation Validation

Record of the audit, the resulting architecture/code/docs, and the
controlled-corpus validation performed for Milestone M ("Test
Intelligence Foundation"). See `latest-summary.md` for the full
narrative -- audit findings, design decisions, corpus results, and gate
results.

**Zero additional provider calls were added by this milestone.** Every
module in `patchfrog/test_intelligence/` is pure/deterministic
(structurally enforced by
`tests/integration/test_test_intelligence_corpus.py::test_test_intelligence_never_imports_a_session_type`
-- no `LLMProvider` import anywhere in the package, no `AsyncSession`
import anywhere in the package either). No live Anthropic call, no live
Gemini call, no OpenAI call, and no Cloud/dashboard work were required
or performed for this milestone.

**Zero additional repository-graph queries or base-commit fetches were
added either** -- both signals are computed entirely from data every
review run already builds (Change Intelligence's `ChangeUnit`s/
`ExpectedCompanionChange`s, and the PR's own already-parsed
`DiffFile`s). See `docs/test-intelligence.md`'s "Architecture" section.

Ground truth for the controlled corpus
(`tests/integration/test_test_intelligence_corpus.py`) is entirely
synthetic, purpose-built git fixture repositories -- never a real
customer PR description or production repository content.

No credentials, installation tokens, webhook secrets, or raw private
source/PR content appear anywhere in this directory.
