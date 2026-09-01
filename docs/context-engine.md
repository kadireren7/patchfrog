# Context Engine

`patchfrog/context/` builds the exact repository context an AI reviewer
sees for one candidate -- deterministic, non-LLM, entirely driven by
Phase 2's repository-intelligence graph (symbols, call edges, imports,
tests) and Phase 3's changed-line/finding data. No embedding, no vector
search, no LLM-driven traversal -- everything here is a fixed, explicit,
auditable rule.

## Pipeline

```
candidate generation (patchfrog.context.candidates)
    -> scoring (patchfrog.context.scoring)
    -> deduplication (patchfrog.context.dedup)
    -> budgeting (patchfrog.context.budgeting)
    -> persisted ContextBundle
```

## Three modes

`ContextConfig` (`patchfrog/context/config.py`) supports three distinct,
explicitly-selectable generation modes -- never hidden behavior overloaded
onto one flag:

1. **Fixed depth 1** (`ContextConfig()`, the historical default) --
   direct callers/callees/tests/imports/parent/sibling only.
2. **Fixed depth 2** (`ContextConfig(graph_depth=2)`) -- additionally
   considers callers-of-callers / callees-of-callees, unconditionally,
   for both directions. Used today mainly as an explicit evaluation
   ablation baseline.
3. **Adaptive 1-hop-first, expand-to-2-when-justified**
   (`ContextConfig(adaptive=AdaptiveContextConfig(enabled=True))`) --
   Milestone E, and the new default for real reviews (see
   `patchfrog/review/service.py`).

A caller picks one of these explicitly; nothing infers a mode from
context.

## Adaptive expansion (Milestone E)

### Why

Depth-1 context is deliberately conservative: fast, small, and correct
for the common case. But some candidates have a real, relevant second
hop -- e.g. `connectOnStartup` calling `reconnectWithBackoff` calling
`computeBackoffMs` -- that depth-1 context structurally cannot see. Fixed
`graph_depth=2` solves that but at a flat cost for every candidate,
whether or not a second hop is actually relevant. Adaptive mode is the
middle ground: pay the (still bounded) depth-2 cost only when a
deterministic structural signal says it's likely to matter.

### How

For every candidate, in order:

1. **Build depth-1 context** exactly as before this milestone.
2. **Tentatively compute depth-2 candidates** for both directions --
   reusing the exact same, already-existing, bounded traversal
   (`ContextCandidateGenerator._call_edge_candidates`) fixed
   `graph_depth=2` mode has always used. This is not a second traversal
   stack; adaptive mode simply decides afterward which of these
   already-computed candidates to keep.
3. **Deterministically decide whether to expand**
   (`patchfrog.context.adaptive.AdaptiveExpansionPolicy`), using only
   structural signals available before any provider call:
   - **A -- call-chain continuation**: a depth-1 caller/callee itself
     has a resolvable second hop.
   - **B -- changed neighbor**: a depth-1 caller/callee is itself on a
     changed line in this diff, and a second hop past it exists.
   - **C -- static/security category relevance**: the candidate's
     associated finding category is one where a direct call relationship
     is already known to matter (memory safety, resource management,
     concurrency, API misuse, security -- mirrors
     `patchfrog.context.scoring`'s existing category-preference table).
   - **E -- thin wrapper/delegator**: the target itself is small and
     delegates to exactly one direct callee, so the real logic is one
     hop further out.

   (Signal **D** -- resolving a module-level constant/config/global a
   depth-2 callee depends on -- is **not implemented**; see "Known
   limitations" below.)

   The decision also picks a direction (`"callers"`, `"callees"`, or
   bounded `"both"` when signals point both ways or direction can't be
   confidently inferred) -- never both directions unconditionally.
4. **Keep only the selected depth-2 candidates**, merge with depth-1,
   and continue through the identical scoring/dedup/budgeting pipeline
   every mode uses.

Every decision is recorded (`AdaptiveContextMetrics`, persisted
nullable-safe on `context_bundles`): whether expansion was attempted, whether it
occurred, why (the exact reasons), which direction, and how many depth-2
candidates were considered vs. selected vs. how many tokens they cost.
Structured logs (`context_adaptive_decision`,
`context_adaptive_expansion_completed`) mirror this without ever logging
source content or secrets.

### Token/line budget

Depth-2 ("expansion") items compete for a **bounded fraction** of the
existing `max_tokens`/`max_lines` ceiling
(`AdaptiveContextConfig.expansion_token_fraction`/`expansion_line_fraction`,
default 0.3 each) -- never simply added on top. A tight budget still
produces a bundle within the exact same `max_tokens`/`max_lines` any
fixed-depth mode would respect; adaptive mode never inflates the maximum
possible bundle size, it only lets a bounded slice of that existing
budget go to depth-2 content instead of being left for depth-1 alone.

### Depth/cycle safety

Depth is hard-capped at 2 in v1
(`patchfrog.context.config.MAX_SUPPORTED_ADAPTIVE_DEPTH`) --
`AdaptiveContextConfig` rejects a higher `max_depth` at construction
time. The traversal itself is bounded and cycle-safe: the original
target/root symbol(s) are excluded from both the depth-1 and depth-2
result sets (fixed in this milestone -- a self-recursive symbol or a
2-cycle previously could re-add the target itself as a "transitive"
neighbor of its own neighbor), depth-1 symbols are excluded from depth-2,
and only `ContextConfig.max_expansion_roots` (default 5, now
config-owned rather than a hardcoded constant) depth-1 nodes are ever
expanded to depth 2 regardless of how connected the graph is. A 3-cycle
never revisits the target because depth is capped at 2 -- the third hop
that would rediscover it is never attempted at all.

### Scoring

Unchanged: transitive (depth-2) candidates already score lower than
direct ones (`patchfrog.context.scoring`'s existing distance penalty and
lower base relationship scores), and a changed-line bonus already
applies uniformly regardless of distance. No adaptive-specific scoring
rule was added -- the existing weights were already sufficient to keep
direct context ranked above transitive context, verified by regression
test rather than assumed.

### Agent Orchestration integration

Unchanged invariant from Milestone D: context is built **exactly once**
per candidate (adaptive or not) into one `CandidateEvidencePackage`, and
both the Correctness and Security specialist agents receive that exact
same package. Adaptive expansion never runs per-role, never gives one
role deeper context than the other, and never lets a specialist trigger
its own context rebuild.

### Config identity / versioning

`CONTEXT_ENGINE_VERSION` bumped 1 -> 2: the target/root exclusion fix
changes what a fixed `graph_depth=2` bundle contains too (not just
adaptive additions), and `max_expansion_roots` moved from a hardcoded
constant into config. `ContextConfig.fingerprint()` also now folds in
the adaptive policy itself, so fixed depth-1, fixed depth-2, and
adaptive configurations always produce distinct fingerprints -- a fixed
bundle is never silently reused as an adaptive one or vice versa.
Incremental-review compatibility (`patchfrog.review_memory.config.compute_memory_compatibility_fingerprint`)
now also includes `CONTEXT_ENGINE_VERSION` -- an audit finding from this
milestone: before this fix, a context-engine change never invalidated
incremental-review candidate skipping at all, even though a skipped
candidate's memory could have been built from an entirely different
context under the new engine.

`REVIEW_PROMPT_VERSION`/`REVIEW_POLICY_VERSION`/`REVIEW_ENGINE_VERSION`
(Agent Orchestration's own versions) are **not** bumped by this
milestone -- neither the specialist prompts nor the review acceptance
policy changed; `ContextConfig`'s own fingerprint/version already fully
protects context identity.

## Known limitations

- **Constants/config/globals are not resolved as context.** If a
  depth-2 callee's behavior depends on a module-level constant (e.g.
  `RETRY_POLICY_MAX_ATTEMPTS`), that constant is not surfaced --
  current repository intelligence has no relationship kind for "depends
  on this constant," and inventing one now would mean guessing at
  semantics rather than reading a real edge. The call-chain part of
  such a case (the actual function-to-function dependency) is resolved
  correctly; the constant itself is a documented gap, not a silent one.
- **Presence of context is not proof of review quality.** Adaptive
  expansion proves the previously-missing symbol is now *in* the
  context sent to the model -- it does not, by itself, prove the model
  uses it correctly. That is a live-provider-quality question,
  deliberately out of scope for this milestone (no live LLM calls were
  made building or testing this).
- **v1 caps at depth 2.** A future milestone could raise this if
  evidence justifies it; this milestone solves the proven 1-hop ->
  2-hop gap only.
- **No embeddings/vector search, ever.** Selection is entirely
  structural; nothing here ranks by semantic similarity.

## See also

- `docs/agent-orchestration.md` -- how specialist agents consume the
  shared evidence package this engine produces.
- `docs/quality-cost-guard.md` -- a later milestone's per-candidate
  effort tier controls the budget/adaptive-mode `ContextConfig` passed
  into this engine (LIGHT: smaller budget, adaptive disabled;
  STANDARD/DEEP: today's adaptive default, up to the full configured
  ceiling). This engine's own ranking/scoring/budgeting/cycle-bound
  logic described above is completely unaffected -- tiering only
  changes what budget/mode it is invoked with, never how it behaves
  once invoked.
- `docs/telemetry-intelligence.md` -- reports this engine's adaptive-
  expansion provenance (`ContextBundleModel.adaptive_expansion_*`,
  depth-2 counts/tokens) per bundle, never snippet content, and never a
  causal-improvement claim.
