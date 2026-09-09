# Agent Handoff / MCP (Milestone T)

Long-term product direction:

> AI coding agents write. PatchFrog verifies.

This does not replace PatchFrog's operative product principle:

> Find fewer, harder, evidence-backed problems.

Milestone T makes the first slice of that direction concrete: turning a
PatchFrog finding into a bounded, structured object a coding agent can
consume (T1), a narrow MCP surface to consume it through (T2), and an
independent loop that checks whether an attempted fix actually resolved
it (T3) -- without turning PatchFrog into a second review engine, a
general repository shell, or an autonomous patch writer.

Full architecture audit, the specific evidence-inclusion tradeoffs, and
every scope decision's reasoning: `validation/agent_handoff/latest-summary.md`.

## What Agent Handoff is

```
PatchFrog Review
    |
    v
Verified Finding (ai_findings)
    |
    v
Finding Handoff (patchfrog.agent_handoff) -- bounded, deterministic projection
    |
    v
MCP (patchfrog.mcp) -- four tools, stdio, read-mostly
    |
    v
Coding Agent -- edits the repository, produces a new commit
    |
    v
PatchFrog Fix Verification (patchfrog.fix_verification)
    |
    +--> FIXED | STILL_PRESENT | INCONCLUSIVE | STALE | ERROR
```

The coding agent edits the repository and produces a new commit.
PatchFrog never edits source code, never commits, and never pushes --
it independently re-evaluates the *original* finding against the *new*
exact commit SHA and reports what the evidence actually shows. A fix
attempt's outcome is never "the agent says it fixed it."

## What Agent Handoff is not

- **Not a second review engine.** `patchfrog.agent_handoff` only ever
  projects fields that are already persisted by the normal review
  pipeline (`ai_findings`, `review_candidates`, `review_runs`,
  `critic_verdicts`). It never re-scores, re-judges, or re-derives a
  finding's truth.
- **Not a hidden reviewer, general repository shell, or arbitrary code
  execution.** MCP exposes exactly four tools (below); none of them run
  an arbitrary shell command, read an arbitrary filesystem path, or
  expose a general query API over the repository.
- **Not a write API into GitHub.** Neither `patchfrog.agent_handoff` nor
  `patchfrog.mcp` nor `patchfrog.fix_verification` import
  `patchfrog.github.client` (the GitHub API client carrying write
  methods) or `patchfrog.publishing` (the review-publication package) --
  enforced structurally by `tests/unit/test_mcp_module_boundaries.py`.
  GitHub publication remains the existing review pipeline's own
  responsibility, completely untouched by this milestone.
- **Not an autonomous patcher.** PatchFrog never proposes or applies a
  fix. The coding agent does that; PatchFrog only verifies.
- **Not a secret retrieval surface.** MCP never exposes provider keys,
  the GitHub App private key, the database URL, the Redis URL, or any
  other credential -- see "Trust and authority" below.
- **Not a new specialist agent.** `patchfrog.fix_verification`'s one
  bounded LLM fallback call reuses the existing provider-neutral
  `LLMProvider`/`ProviderRequest` interface directly; no new
  `patchfrog.review.agents.roles.AgentRole` is added.
- **Not `docs/agent-orchestration.md`.** See that document's own "See
  also" section for the distinction between PatchFrog's internal review
  specialists and this milestone's external-agent handoff.

## T1 — Finding Handoff Schema

`patchfrog.agent_handoff.domain.FindingHandoff` -- a deterministic
projection of one persisted `ai_findings` row (plus its candidate, review
run, and any critic verdict), built by `patchfrog.agent_handoff.service.
AgentHandoffService.build_handoff`. No LLM call, no new persistence: if a
finding doesn't have enough stable evidence, the service returns an
honest `HandoffUnavailableReason` rather than inventing anything.

**The single most important scope decision, found during this
milestone's own audit**: none of the Intelligence layers (J Change, K
Contract, L Intent, M Test, N Historical, O Repository Learnings, P
Trajectory, Q Cross-PR, R Cross-Repo) persist evidence attributable to one
specific finding -- every one of them only ever produces a **whole-review-
run** aggregate summary (counts plus a rendered story/text block on
`review_runs`), never a per-finding row. The handoff therefore cannot
include K/L/M/N/O/P/Q/R evidence for a specific finding in v1 -- there is
nothing persisted to read. Rather than add new cross-cutting persistence
to nine existing packages (well outside "the narrowest safe scope"), this
is documented as an explicit, honest v1 limitation.

**Also honestly absent**: `executable_verification_evidence`. Milestone
S/S6's `ExecutableVerificationEvidence` is deliberately ephemeral --
computed once per candidate, fed into the critic's prompt, and never
persisted past the review run (only run-level counts survive). The field
exists on `FindingHandoff` (typed, always `None` in v1) so the wire shape
is stable for a future milestone that does persist it, without ever
fabricating a value today.

**What the handoff does include**: title/message (identification),
reasoning_summary (mechanism), impact/suggested_fix (nullable, never
fabricated), category/severity/confidence, exact file/line location and
qualified symbol name, up to 5 bounded evidence quotes (already-validated,
already-source-checked -- never freshly re-excerpted), `corroborated_by_static`
plus the specific static-finding ids it corroborates against (static
findings, unlike the LLM Intelligence layers, *are* individually
addressable), the critic's own `reasoning_summary` when a verdict exists,
a deterministically re-derived `suggested_verification_target` (the same
`patchfrog.executable_verification.eligibility.determine_verification_target`
primitive Milestone S/S6 use, reconstructed from the persisted candidate,
never assumed from the original review), and whether/where it was already
published to the PR.

**Deliberately excluded** (never fields on `FindingHandoff`, enforced by a
dedicated unit test): token budgets, critic call counts, agent-role call
counts, raw provider-internal confidence, private prompt content, and any
Trajectory/Cross-PR/Cross-Repo internal-only signal.

**Redaction**: every free-text field passes through
`patchfrog.review.redaction.redact_secrets` as defense-in-depth. The
primary defense, as documented in that module, is that only
already-validated, already-bounded repository-derived text ever reaches a
persisted finding in the first place -- this redaction pass proves the
serialization path, not terminal/UI/local exposure.

**Identity**: `handoff_id = sha256(repository_id | finding_id |
review_run_id | original_commit_sha)` -- deterministic and stable; two
different findings can never collide, and it is never derived from
mutable prose. `FINDING_HANDOFF_SCHEMA_VERSION = 1` is a real, externally-
consumed wire contract (an MCP client may persist/parse this shape
outside PatchFrog's own process); bump only for an incompatible field
add/remove/reinterpret.

**Exact-head binding**: a handoff always belongs to `original_commit_sha`
-- it is never mutated to pretend it came from a later head. A fix
attempt's `candidate_fix_commit_sha` is a completely separate field on a
completely separate record (`FixAttempt`, below); provenance A -> B is
always explicit, never silently rebound.

## T2 — MCP Server

`patchfrog.mcp.server.PatchFrogMCPServer`, built on the official Model
Context Protocol Python SDK (`mcp`, MIT license, `mcp>=1.29,<2.0` --
already present transitively via `semgrep`'s own dependency in this
repository's environment, now declared as a first-class PatchFrog
dependency). **stdio transport only** in v1 -- no HTTP/SSE, no public
network listener; a narrower local boundary is the right default for a
self-hosted engine, and it works naturally with Claude Code/Codex/Cursor/
Desktop out of the box. Launch:

```
python -m patchfrog.cli mcp serve
```

**Exactly four tools** (no MCP resources in v1 -- they would duplicate
the same content a second way, which Part AK of this milestone's own spec
explicitly warns against):

| Tool | Reads/writes |
|---|---|
| `list_findings(repository_full_name, pull_request_number?, review_run_id?, category?, severity?, limit<=50)` | Read-only. Bounded to one specific, already-completed review run -- identified directly or resolved from a PR number. Never "every finding in the repository." |
| `get_finding_handoff(repository_full_name, finding_id)` | Read-only. Returns `FindingHandoff` plus a small summary of any known fix attempts for that finding. |
| `start_fix_attempt(repository_full_name, finding_id, candidate_fix_commit_sha, handoff_id?)` | Writes exactly one new `fix_attempts` row (or returns the existing one -- idempotent), then runs verification. Never a GitHub write, never a file write. |
| `get_fix_attempt(repository_full_name, fix_attempt_id)` | Read-only. Polls status/result. |

**Adaptation from a literal `start_fix_attempt(handoff_id, ...)`
signature**: `handoff_id` is a one-way hash with no lookup table (T1 above
-- a handoff is always cheaply re-derivable, so persisting one just to
look it up later would be exactly the "table for architecture aesthetics"
anti-pattern this milestone's own spec warns against). The tool instead
takes `finding_id` as the primary identity and re-derives the handoff
fresh, server-side; an optional `handoff_id` is checked as an integrity
guard and the call fails closed on a mismatch.

## Trust and authority

**Local trust model (v1 has no multi-user authorization).** stdio access
inherits whatever Unix user runs `python -m patchfrog.cli mcp serve` --
this is full local trust, not a security boundary between different
callers of the same process. Every tool still requires an explicit
`repository_full_name` and independently re-validates that any
`finding_id`/`fix_attempt_id` actually belongs to that repository before
returning anything (defense against ID-guessing/cross-repo leakage within
one operator's own multi-repository database, not a multi-tenant
boundary) -- a cross-repository lookup returns the exact same
`"not_found"` shape as a genuinely missing id, never a distinct "wrong
repository" error that would confirm the id exists elsewhere. Future
Cloud-hosted MCP authentication/authorization is explicitly out of scope
here (see "Source-available / Cloud boundary" below).

**Read/write authority.** MCP itself never writes source code, never
`git commit`s, never `git push`es, never writes to GitHub, never deletes
a finding, never changes review policy, never touches `.patchfrog.yml`,
never touches provider configuration, never executes an arbitrary shell
command, and never reads an arbitrary filesystem path. Its one write path
is `FixVerificationService.start_fix_attempt`, which writes exactly one
narrow `fix_attempts` row.

**Rate/cost bounds** (`patchfrog.fix_verification.domain`):
`MAX_ACTIVE_FIX_ATTEMPTS_PER_REPOSITORY = 5`,
`MAX_FIX_ATTEMPTS_PER_FINDING = 20` -- both enforced before a new attempt
is created; an existing (already-started) attempt for the same
`(handoff_id, candidate_fix_commit_sha)` pair is always returned instead
of creating new work (idempotency, not a cap violation).

## T3 — Fix Verification Loop

`patchfrog.fix_verification.service.FixVerificationService`. A
`FixAttempt` is a first-class, durably persisted record (`fix_attempts`,
migration `0029_fix_attempts`) -- justified because verification is
asynchronous relative to an MCP client (it may reconnect and poll), and
idempotency needs a durable identity. Unique index on `(handoff_id,
candidate_fix_commit_sha)`.

**Algorithm -- deterministic-first, no unconditional provider call**:

1. **Ancestry.** Prove `candidate_fix_commit_sha` is a real descendant of
   `original_commit_sha` (`patchfrog.repository.ancestry.
   verify_ancestor_with_diff`, unmodified, the exact same Phase-7
   primitive already used to guard incremental review memory reuse). Not
   provable -> `STALE`. An identical SHA is explicitly allowed as a
   no-op comparison, not rejected.
2. **File-level change detection.** If the flagged file did not change
   at all between the two commits -> `STILL_PRESENT` (the flagged code
   is byte-identical; it cannot have newly become fixed). This is the
   cheapest, strongest deterministic signal and skips every further step
   when it fires.
3. **Static re-check**, only if the finding was `corroborated_by_static`:
   re-run the *original* analyzer (`patchfrog.analysis.analyzers.registry`),
   scoped to exactly one file -- never `StaticAnalysisService`'s full
   repository indexing/persistence orchestration, since this is a
   bounded, ephemeral re-check for one finding, not a new analysis run.
4. **Executable Verification re-run**, only if eligible (the same
   `determine_verification_target` primitive T1 uses): dispatches to the
   existing S6 verifier (only when `PATCHFROG_VERIFIER_ENABLED`,
   otherwise skipped -- never executed in-process) against the
   **candidate SHA only**. It does *not* also re-execute against the
   original SHA to manufacture a "before" data point -- that evidence was
   never persisted (T1's own gap above), and doing so would be an extra,
   currently-unbounded sandbox execution per fix attempt; every result
   documents this explicitly as a limitation rather than silently
   pretending a true A/B comparison happened.
5. **Combine.** Any contradicting deterministic signal (static rule still
   fires, or Executable Verification still confirms failure) wins outright
   -> `STILL_PRESENT`, conservative by design. Any confirming signal with
   nothing contradicting -> `FIXED`. No deterministic signal at all (not
   statically corroborated, not EV-eligible, and the file *did* change) ->
   exactly one bounded LLM call (`patchfrog.fix_verification.critic`,
   reusing the existing `LLMProvider` interface, never
   `patchfrog.review.critic.CriticService` -- that service asks "is this
   *new* finding real," a materially different question from "does this
   *specific, already-confirmed* finding still hold"). No provider
   configured -> `INCONCLUSIVE`, never a guess.

Any infrastructure failure (clone/network/git error) at any point ->
`ERROR`, always kept distinct from the four semantic outcomes above.

**`FIXED` is never "the agent says it fixed it," never "the changed line
disappeared," and never "a test passes" on its own** -- it is always the
combination above. **A single Executable Verification `PASSED` is not
treated as universal proof** either: it is one signal among several, and
a contradicting static-recheck result still wins.

**Finding lifecycle.** A `FixAttempt.status == FIXED` result is never
written back onto `ai_findings` or `review_memory_findings` -- those keep
meaning exactly what they already meant. `FixAttempt` is its own,
additive, independently query-joinable record, never a mutation of
finding truth.

`FIX_VERIFICATION_VERSION = 1` -- a new, independent semantic contract
for what these five outcomes mean, distinct from `REVIEW_ENGINE_VERSION`
(normal-review semantics, untouched by this milestone) and
`VERIFIER_PROTOCOL_VERSION` (the S6 wire contract step 4 reuses
unmodified).

## Client compatibility

The handoff schema and MCP tool surface are provider/client-neutral --
no Claude-specific (or any other vendor-specific) field anywhere in the
domain model. Compatibility target: any MCP-capable client (Claude Code,
Codex, Cursor, or another MCP host) speaking the standard stdio
transport.

## Source-available / Cloud boundary

Everything in this document (the handoff schema, the MCP server, the
`FixAttempt` domain, the Fix Verification engine, evidence serialization,
the generic client-neutral protocol) is source-available, in the public
engine, exactly like the rest of PatchFrog. A future private Cloud may
add a *hosted* MCP endpoint, account auth, tenant authorization, Cloud-
specific rate limits, an agent marketplace, agent billing, and enterprise
access controls -- none of that exists in this repository, and none of it
is implemented by this milestone. See `docs/product-boundary.md`.

## Limitations

- No per-finding K/L/M/N/O/P/Q/R Intelligence evidence in the handoff
  (nothing persisted to read -- see T1 above).
- No Executable Verification re-execution against the original SHA in
  the fix-verification loop (see T3 step 4 above) -- every `FixAttempt`
  result's `limitations` field says so explicitly.
- No MCP resources, no HTTP/SSE transport, no multi-user authorization.
- No new Prometheus metrics for MCP/fix-attempt counts in v1 -- MCP is a
  standalone local process outside the worker's own
  `PROMETHEUS_MULTIPROC_DIR` aggregation topology; the `fix_attempts`
  table itself is directly queryable for the same counts.
