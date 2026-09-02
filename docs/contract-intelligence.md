# Contract & Blast Radius Intelligence

`patchfrog/contract_intelligence/` extends `patchfrog/change_intelligence/`
(Milestone J) with a deterministic answer to a narrower, harder question:
**when a boundary changes, which real consumers may still assume the old
contract?** It is not a second parallel intelligence stack -- see "Reuse,
not duplication" below. This document does not introduce a new phase
number, and it does not change what PatchFrog is: **a reviewer/verifier
that finds fewer, harder, evidence-backed problems** -- never architecture
theater, never a numeric risk score, never automatic publication from a
heuristic.

**PatchFrog does not infer contract relationships from imagination.
Contract impact edges must be grounded in repository/index evidence.**

## What PatchFrog considers a contract

A `ContractDescriptor` is produced only for a symbol with **real evidence
that something consumes it** -- at least one resolved caller in the
repository graph. An internal leaf helper with zero callers never becomes
a contract, no matter how much its signature changes (spec section 4).

## Supported contract kinds

`patchfrog.contract_intelligence.domain.ContractKind` keeps all six
values from the milestone brief (`FUNCTION`, `SCHEMA`, `CONFIGURATION`,
`PERSISTENCE`, `EVENT`, `PUBLIC_INTERFACE`) for forward documentation,
but **this milestone's detection logic only ever constructs `FUNCTION`
descriptors, Python only.**

- **`FUNCTION` (Python)** -- robustly supported. `SymbolModel.signature`
  is already the real, verbatim `def`/`async def` header text (see
  `patchfrog.parsing.python._function_signature`); a new deterministic
  tokenizer (`patchfrog.contract_intelligence.function_signature`) parses
  it structurally -- parameter names, positional/keyword-only/
  positional-only markers, `*args`/`**kwargs`, defaults, and the return
  annotation, all kept as opaque text, never a resolved/interpreted type.

### Deferred (and why)

- **`FUNCTION` (C/C++)** -- the grammar (pointers, references, `const`,
  templates, comma-bearing default expressions, function-pointer
  parameters) is materially riskier to hand-parse correctly than
  Python's; a wrong parse would risk a false-positive contract delta,
  which the product principle explicitly forbids.
- **`SCHEMA`** -- no schema/DTO model exists in the index (no
  Pydantic-field/dataclass-field extraction, no OpenAPI awareness).
  Detecting "a schema field was removed" would require guessing which
  classes are "schemas" -- exactly the OpenAPI-product-work /
  arbitrary-categorization the spec forbids.
- **`CONFIGURATION`** -- `ChangeKind.CONFIGURATION` (Milestone J) is a
  path-pattern heuristic, not a structured config-key/default inventory;
  there is no "loader reads key X" relationship in the index.
- **`PERSISTENCE`** -- no ORM-field/column-mapping model exists.
- **`EVENT`** -- no event producer/consumer relationship is indexed.
- **`PUBLIC_INTERFACE`** -- Python parsing never populates `visibility`
  (confirmed in `patchfrog/parsing/python.py`); there is no `__all__`/
  export-list signal independent of "has a real caller," which
  `FUNCTION` already uses as its own boundary gate.

Full reasoning: `validation/contract_intelligence/latest-summary.md`
section 1.

## Base/head comparison

No second repository index is ever created. The PR's `base_sha` (already
fetched from GitHub for every review, just not previously threaded
through) is used to fetch **only** the files containing a contract-
eligible changed candidate, via the same bounded, read-only primitive
Phase 7 evidence revalidation already uses
(`patchfrog.repository.file_contents.read_files_at_commit` in
production; a direct local `git show` for CLI/local review). The fetched
content is parsed **in-memory, with the exact same parser already used
for indexing** (`patchfrog.parsing.registry`) -- nothing is written to
the database. A symbol absent from the base parse (newly introduced) or
whose base content couldn't be fetched produces no delta -- never a
guess. Anchored to the exact `base_sha`/`commit_sha` of the review run;
no network mutation; no LLM.

## Contract deltas and breaking characteristics

`patchfrog.contract_intelligence.delta.diff_signatures` compares two
parsed signatures **by parameter name**, never positional index (a
reordering of unchanged, already-present parameters is not flagged --
see Limitations), and produces a tuple of
`BreakingCharacteristic` values -- never a BREAKING/SAFE verdict, never
a numeric compatibility score:

| Characteristic | Consumer-breaking? |
|---|---|
| `REQUIRED_PARAMETER_ADDED` | yes |
| `PARAMETER_REMOVED` | yes |
| `DEFAULT_REMOVED` | yes |
| `RETURN_BECAME_OPTIONAL` | yes |
| `SYNC_TO_ASYNC` / `ASYNC_TO_SYNC` | yes |
| `OPTIONAL_PARAMETER_ADDED` | no (backward-compatible) |
| `DEFAULT_ADDED` | no (backward-compatible) |
| `RETURN_BECAME_REQUIRED` | no (a producer/builder concern, not a caller one) |
| `RETURN_ANNOTATION_CHANGED` | no (generic, non-optionality annotation change) |

`ContractDelta.is_potentially_breaking` is `True` iff at least one
characteristic is in the "yes" set above
(`patchfrog.contract_intelligence.domain.BREAKING_CHARACTERISTICS`).
"Optional-shaped" return-annotation detection (`RETURN_BECAME_OPTIONAL`/
`RETURN_BECAME_REQUIRED`) is syntax-only (`Optional[...]` or a top-level
`... | None` union member) -- never a resolved/imported-type check. A
quoted forward-reference annotation (`-> "dict | None"`) is not
recognized as optional-shaped (the literal quote characters are part of
the preserved text) -- it falls through to the generic
`RETURN_ANNOTATION_CHANGED` bucket instead, a safe false-negative, never
a false positive.

## Blast radius

Reuses `patchfrog.change_intelligence.affected_surface.derive_affected_surface`
**directly**, not reimplemented -- for a contract-bearing symbol, its
owning `ReviewCandidate` is passed straight into that existing function,
inheriting its exact bounds (`MAX_GRAPH_DEPTH=2`,
`MAX_FANOUT_PER_SYMBOL=50`, `MAX_AFFECTED_SURFACE_PER_UNIT=25`) and
DIRECTLY_DEPENDENT/INDIRECTLY_AFFECTED/TEST classification verbatim.

## Stale-consumer candidates

A `patchfrog.change_intelligence.domain.ExpectedCompanionChange` (reason
code `CONTRACT_CONSUMER_NOT_UPDATED`, a new member added to the
*existing* enum, not a parallel type -- spec section 9) is produced only
when **all** of:

1. a real `ContractDelta` exists
2. its `is_potentially_breaking` is `True`
3. a real, currently-resolved caller of the contract symbol exists
   (`RepositoryQueryService.get_callers` against the exact HEAD index)
4. that caller was not itself changed in this diff
5. the caller is named specifically (an unresolved call target never
   produces a candidate)

**Never auto-published.** Exactly like every other `ExpectedCompanionChange`,
this is a candidate the existing reviewer/critic pipeline must
independently verify (or reject) before anything reaches GitHub.

## Change Map integration

No second diagram system. When contract stale-consumer candidates exist,
they are simply included in the same `expected_companions` tuple already
passed to `patchfrog.change_intelligence.change_map.render_change_map`
(which already renders any `MISSING`-status companion under "Expected
but missing" regardless of reason code) -- the map is re-rendered once,
at the review-service integration point, with the combined companion
set. Diagram *eligibility* is untouched: it still depends purely on real
`ChangeUnit` connectivity (spec section 10 -- "Existing diagram bounds
remain authoritative").

## Contract Story

`patchfrog.contract_intelligence.story.build_contract_story` produces at
most two extra sentences, appended to the existing Change Story text --
never a separate publication block, never a separate persisted column.
Empty when there is no potentially-breaking delta. Example:

> This PR changes the contract of `save` in a way that may affect
> callers. 1 current consumer (`process`) was not touched in this diff
> and may still assume the old contract.

## Review pipeline integration

Computed once per run, in `PullRequestReviewService._execute_and_persist`,
immediately after Change Intelligence (whose already-built `ChangeUnit`s
it reuses for `change_unit_id` attribution -- never a second grouping
pass). Per-candidate evidence
(`patchfrog.contract_intelligence.evidence.evidence_text_for_candidate`)
is attached only to the exact candidate that is the *source* of a real
contract delta, as an optional `<contract_intelligence>` prompt section
(`REVIEW_PROMPT_VERSION` 4 -> 5) -- empty, and thus byte-identical, for
every other candidate. `REVIEW_POLICY_VERSION`/`REVIEW_ENGINE_VERSION`
are **not** bumped -- nothing about finding survival or orchestration
changed. **No new agent role** -- Correctness and Security remain the
only specialists.

**Zero additional provider calls.** Structurally proven
(`test_contract_intelligence_never_calls_a_provider`): no `LLMProvider`
import anywhere in `patchfrog/contract_intelligence/`. The only I/O this
package performs is the bounded, read-only base-commit file fetch
described above.

## Persistence

`review_runs` gained five nullable-default count columns (migration
`0019_contract_intelligence`): `contract_delta_count`,
`contract_kind_counts`, `potentially_breaking_delta_count`,
`impacted_consumer_count`, `stale_consumer_candidate_count`. **No new
text columns** -- the Contract Story addendum is folded into the
existing `change_story` column, and the Contract Map reuses the existing
`change_map_text`/`change_map_rendered`/`change_map_node_count` columns
(the *same* map, not a second one). This mirrors Milestone J's own
"persist only bounded derived text, justify it" precedent: both
`change_story`/`change_map_text` are needed because publication
(`apps/worker/tasks/publish_review.py`) runs as a separate,
independently-retriable Celery task from review generation, and
recomputing on every retry would be wasteful. Neither column, before or
after this milestone, ever contains raw source, diff bodies, prompts, or
a provider response.

## Telemetry and versioning

`patchfrog.telemetry.domain.ContractIntelligenceTelemetry` (counts
only -- no signature text, no Contract Story prose) is a new field on
`ReviewTelemetrySnapshot`. Because `patchfrog.telemetry.reporting.snapshot_to_dict`
exports every dataclass field via `dataclasses.asdict`, this is a real
exported-JSON-shape change, so `TELEMETRY_SCHEMA_VERSION` is bumped
2 -> 3 (this milestone does not repeat Milestone J's initial oversight
of skipping that bump for an additive field). Historical rows export
`contract_intelligence` with explicit zero/default values.

`CONTRACT_INTELLIGENCE_VERSION = 1` is introduced as this package's own
semantic-identity version. `CHANGE_INTELLIGENCE_VERSION` is **unchanged**
-- Change Intelligence's own grouping/affected-surface/companion logic
is untouched; only a new enum member was added to a type it already
owned, consumed by a different package.

## Limitations

- Comparison is by parameter **name**, not positional index -- a pure
  reordering of already-present, unchanged parameters is never flagged.
  Reasoning correctly about positional-call-site breakage from
  reordering would need real call-site argument-shape evidence this
  index doesn't have.
- `*args`/`**kwargs` *presence* changes are not compared -- deliberately
  out of scope this milestone.
- A default value containing a `lambda` with more than one parameter
  (rare) is handled via explicit lambda-depth tracking in the tokenizer;
  any other shape the tokenizer doesn't model fails the *whole*
  signature parse closed (returns no delta) rather than risk a silently
  wrong parameter split becoming a false-positive delta.
- C/C++ function contracts are deferred (see above).
- A quoted forward-reference return annotation (`-> "dict | None"`) is
  not recognized as optional-shaped -- falls through to the generic,
  non-breaking `RETURN_ANNOTATION_CHANGED` bucket instead (a
  false-negative, never a false positive).
- Missing-consumer candidates are heuristic evidence, not proof -- they
  must survive the existing reviewer/critic pipeline like any other
  finding before ever reaching GitHub.
- Says nothing about *intent* -- whether a missing consumer update is
  actually a bug is left to the existing reviewer/critic, never
  fabricated here.
