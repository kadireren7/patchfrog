# Change Intelligence Foundation

`patchfrog/change_intelligence/` introduces PatchFrog's first "Change
Intelligence" layer: instead of treating a PR as a flat list of changed
files/symbols, PatchFrog now groups those changes into logical,
behavioral units, derives their affected surface from the repository
graph, and flags dependent surfaces that real evidence suggests should
have changed but didn't. This document explains that layer. It does not
introduce a new phase number, and it does not change what PatchFrog is:
**a reviewer/verifier that finds fewer, harder, evidence-backed
problems** -- never generic PR summarization, architecture theater, or
diagram spam.

**PatchFrog does not infer an architecture diagram from imagination.
Every rendered relationship is derived from repository-index evidence.**

## Why: a PR is a set of logical changes, not a set of files

Before this milestone, PatchFrog's understanding of "what changed"
stopped at `ReviewCandidate` -- one changed symbol or module region at a
time, reviewed independently. That's sufficient for finding bugs *in* a
changed symbol, but it can't answer "does this PR consistently implement
one coherent change?" or "did the caller/test that depends on this
change get updated too?" Change Intelligence answers exactly those two
questions, deterministically, and feeds the answer back into the
existing reviewer as bounded extra evidence -- never as a new score, a
new agent, or a new LLM call.

## Reusing the existing graph, not building a second one

Every primitive Change Intelligence reads already existed:
`patchfrog.intelligence.queries.RepositoryQueryService` (callers,
callees, imports, likely tests), `patchfrog.persistence.models.code_index`
(`SymbolModel.parent_symbol_id`, `CallReferenceModel`,
`ImportReferenceModel`), and `patchfrog.review.candidates.ReviewCandidateGenerator`'s
already-diff-driven `ReviewCandidate` list. Change Intelligence adds no
new graph model, no new indexing pass, and no new lifecycle -- it is a
pure function of already-persisted index state plus one review run's
already-generated candidates, computed once per run and persisted as a
few additional nullable-default columns on the existing `review_runs`
row (see `validation/change_intelligence/latest-summary.md` for the
full audit that established this before any code was written).

## The domain model

`patchfrog.change_intelligence.domain`:

- **`ChangeUnit`** -- one logical behavior/change (e.g. "retry policy
  introduction," "API schema migration"), never one file. Holds its
  `changed_candidates` (the `ReviewCandidate`s that make it up), a
  `change_kind`, and its `affected_surface`.
- **`ChangeKind`** -- a small, evidence-based taxonomy: `BEHAVIOR`,
  `CONTRACT`, `PERSISTENCE`, `CONFIGURATION`, `TEST`, `INFRASTRUCTURE`,
  `MIXED`. Never 30 categories, never an LLM guess -- see
  `patchfrog.change_intelligence.change_kind` for the exact path-marker
  and graph-signal rules, checked in a fixed, deliberate order (e.g.
  infrastructure markers before the generic config-extension check, so
  `.github/workflows/ci.yml` classifies `INFRASTRUCTURE`, not
  `CONFIGURATION`).
- **`AffectedSymbolRef`** -- one node in a `ChangeUnit`'s affected
  surface, tagged `DIRECTLY_CHANGED` / `DIRECTLY_DEPENDENT` /
  `INDIRECTLY_AFFECTED` / `TEST`, always carrying a `reason` string
  tracing back to the real graph edge that put it there.
- **`ExpectedCompanionChange`** -- one candidate dependent surface,
  `OBSERVED` (it changed too) or `MISSING` (it didn't), always tied to a
  real caller or test edge -- never a heuristic guess about what
  "should" exist.

## Deterministic grouping

`patchfrog.change_intelligence.grouping.group_into_change_units` unions
changed candidates via a from-scratch Union-Find over three real graph
signals: call edges (caller/callee), symbol containment
(parent/child/sibling), and direct file-level imports. It never groups
by shared directory, never merges everything into one unit, and a
module-region candidate (no parser symbol, e.g. a docs-only change)
never merges with anything. Output ordering is fully deterministic
(`(file_path, start_line, name)`), so the same diff always produces the
same units in the same order -- no provider call, no randomness.

## Affected surface

`patchfrog.change_intelligence.affected_surface.derive_affected_surface`
walks the same query primitives the existing Context Engine uses,
bounded to depth 2 (`MAX_GRAPH_DEPTH`), a fan-out cap per symbol
(`MAX_FANOUT_PER_SYMBOL`), and a per-unit node cap
(`MAX_AFFECTED_SURFACE_PER_UNIT`) -- never a full-repository traversal.
Depth-1 callers/callees become `DIRECTLY_DEPENDENT`; depth-2 becomes
`INDIRECTLY_AFFECTED`; `likely_tests_for_file` results become `TEST`.

## Expected companion changes

`patchfrog.change_intelligence.companions.derive_expected_companions`
implements exactly two graph-grounded heuristics -- deliberately not a
larger taxonomy:

1. **Caller staleness** -- a real caller of a changed symbol that itself
   wasn't touched in this diff.
2. **Test staleness** -- a real test file (`likely_tests_for_file`) of a
   changed file that itself wasn't touched.

Both cover every example in the spec (schema+serializer, config+loader,
signature+caller, new error state+handler) without inventing a
`SymbolKind.SERIALIZER`-style classification this codebase has no real
evidence for. **These candidates never auto-publish.** They flow into
the existing reviewer as bounded evidence
(`patchfrog.change_intelligence.evidence.evidence_text_for_candidate`)
and must survive the same validation/critic/confidence pipeline as
every other finding before anything reaches GitHub.

## Conditional Change Map

`patchfrog.change_intelligence.change_map.should_render_change_map` is
purely deterministic -- never an LLM judgment call. A unit is eligible
only when its changed+affected nodes span **at least 3 distinct
`(file, symbol)` pairs across at least 2 distinct files**. That
threshold is what separates a genuine cross-component change from a
one-file/one-function edit:

| Case | Diagram? |
|---|---|
| Docs-only PR | No -- no symbol nodes at all |
| Isolated one-function fix, no resolvable callers | No -- 1 node |
| Simple rename with a same-file caller | No -- 1 file |
| Multiple symbols confined to one file | No -- fails the 2-file rule |
| API → service → repository change | Yes -- 3 nodes, 3 files |
| Worker → service → persistence change | Yes |
| Schema + serializer + consumer change | Yes |
| Huge, highly-connected graph | Yes, but bounded/truncated |
| Two disconnected logical changes | Each evaluated independently; never one fabricated combined diagram |

At most one `ChangeUnit` per report ever renders a map -- the most
node-rich eligible unit, deterministic tie-break by unit id. See
`tests/unit/test_change_intelligence_change_map.py` for all nine
mandatory spec cases.

### Format

A bounded, grouped Markdown bullet list (Changed / Directly dependent /
Indirectly affected / Tests / Expected but missing), not an ASCII/
Mermaid node-and-arrow diagram -- laying out an arbitrary graph as 2D
ASCII art is itself a source of bugs (overlaps, ambiguous crossing
lines) this milestone doesn't need to take on, and a grouped list
already expresses the same semantics. Hard bounds:
`MAX_CHANGE_MAP_NODES = 12`, `MAX_CHANGE_MAP_EDGES = 16`; truncation is
always explicitly noted in the rendered text, never silent. Every label
is sanitized (`patchfrog.publishing.marker.sanitize_untrusted_text`) and
uses a concise symbol/module name, never an internal id or a giant
qualified name.

## Risk / attention areas

`patchfrog.change_intelligence.attention.derive_attention_areas`
produces a small, bounded, explainable list of areas worth a reviewer's
extra attention (contract-shaped change, persistence-shaped change,
wide fan-out, security-sensitive naming, missing companion candidates)
-- deliberately **not a numeric PR score**. This is context for review,
not a finding published on its own.

## Change Story

`patchfrog.change_intelligence.change_story.build_change_story` produces
a 2-4 sentence deterministic description from the real `ChangeUnit`s and
missing companions -- no LLM call, no marketing language, no confidence
theatrics, never a claim not directly supported by the graph evidence
above.

## Review pipeline integration

Computed once per run in `patchfrog.review.service._execute_and_persist`,
immediately after candidate generation, on the full pre-narrowing
candidate set. For each candidate, `evidence_text_for_candidate` returns
a short (at most a few lines), already-bounded evidence block -- empty
string for the common case where nothing is worth surfacing -- which
`patchfrog.review.prompt.build_agent_prompt` includes as an optional
`<change_intelligence>` prompt section only when non-empty. This is why
`REVIEW_PROMPT_VERSION` was bumped (3 → 4): the template shape itself
changed, even though most candidates see a byte-identical prompt.
`REVIEW_POLICY_VERSION`/`REVIEW_ENGINE_VERSION` were **not** bumped --
nothing about what survives to a final finding, or how execution is
orchestrated, changed. **No new agent role was added** -- Correctness
and Security remain the only specialists; Change Intelligence is an
evidence primitive they can optionally use, not a third voice.

Quality + Cost Guard remains authoritative: the per-candidate evidence
text is already small enough to apply uniformly across effort tiers
without a LIGHT-tier candidate suddenly receiving a large payload.

**Zero additional provider calls.** Every module in this package is
pure/deterministic (`test_change_intelligence_never_calls_a_provider`
structurally proves no `LLMProvider` import exists anywhere in the
package). If the already-running specialist reviewer can use the
result, it does; nothing here triggers a new Anthropic or Gemini call.

## Publication

`patchfrog.publishing.body.format_summary_body` places (only on the
genuine-findings path, never the clean-review path) the Change Story
and, when eligible, the Change Map ahead of the findings list -- per the
spec's suggested order. `PublicationConfig.post_clean_summary`'s
existing semantics (see `docs/external-beta.md` and the Milestone I
carried-forward-publication fix) are completely untouched: the
clean-review body is a separate, fixed template that never reads
Change Intelligence text at all.

## Telemetry and versioning

`patchfrog.change_intelligence.telemetry.summarize_for_persistence`
produces a bounded summary (counts, kind breakdown, the already-bounded
Change Story/Change Map text) persisted onto new nullable-default
`review_runs` columns (migration `0018_change_intelligence`). The
telemetry snapshot (`patchfrog.telemetry.domain.ChangeIntelligenceTelemetry`)
carries only counts -- never Change Story/Change Map prose, which stays
a publication-only concern.

`TELEMETRY_SCHEMA_VERSION` **was** bumped, 1 -> 2. Initial correction:
this doc originally reasoned by analogy to `review_feedback` ("purely
additive, no bump needed"), but that analogy doesn't actually hold --
`review_feedback` was introduced in the same commit that introduced
`TELEMETRY_SCHEMA_VERSION = 1` itself (`patchfrog.telemetry.reporting.snapshot_to_dict`
exports every `ReviewTelemetrySnapshot` field via `dataclasses.asdict`),
so there was never a real precedent of an additive field shipping
*without* a bump to compare against. `change_intelligence` genuinely
changes the exported JSON shape (a new top-level key), which is exactly
what the version docstring in `patchfrog.telemetry.domain` says the
version tracks -- so it's bumped like any other shape change. Historical
rows (persisted before this milestone) export `change_intelligence`
with explicit zero/default values, never a fabricated Change Story or
Change Map -- see `tests/integration/test_telemetry_collector.py::test_historical_row_without_change_intelligence_exports_defaults_under_schema_2`.

`CHANGE_INTELLIGENCE_VERSION = 1` is introduced as this package's own
semantic-identity version, separate from the review versions above --
bump it if grouping/affected-surface/companion-detection semantics ever
change materially.

## Limitations

- Call resolution (`patchfrog.intelligence.resolution`) reliably
  resolves `from module import name` + bare `name(...)` calls. Python
  attribute-access calls (`module.function()`) and OOP-style
  `Instance().method()` chains are not proven to resolve, so a
  companion candidate tied to that call shape may be missed -- never a
  false positive, only a potential false negative.
- Missing companion candidates are heuristic evidence, not proof; they
  must survive the reviewer's own validation/critic pipeline like any
  other claim before ever reaching GitHub.
- The Change Map never expresses multi-hop transitive detail beyond
  depth 2, and large graphs are truncated (always noted explicitly,
  never silently).
- This layer says nothing about *intent* (was the missing change a bug,
  or genuinely unnecessary?) -- that judgment is left to the existing
  reviewer/critic, not fabricated here.
