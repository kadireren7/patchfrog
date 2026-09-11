# PatchFrog Roadmap

This is the authoritative roadmap. If another document appears to describe a
different order, this file wins -- update it rather than maintaining two.

Long-term product thesis (direction, not current capability -- see the
"Long-term" section at the bottom before treating any of this as available
today):

> AI coding agents write. PatchFrog verifies.

This does not replace PatchFrog's current product principle, which remains
the operative one at every step below:

> Find fewer, harder, evidence-backed problems.

The long-term architecture prefers stronger verification, stronger evidence,
better feedback calibration, agent interoperability, and explainable merge
decisions -- over more review comments, more agents, more intelligence
layers for their own sake, arbitrary numeric scores, or speculative
architecture inference.

**False-positive discipline governs every milestone below.** PatchFrog's
north star is not comment volume -- no milestone here should ship a feature
whose main output is more review comments. Every milestone in this roadmap
must be able to answer: what real user/reviewer problem does it solve; why
does PatchFrog miss it today; what concrete issue can the new evidence
expose or verify; can it be evaluated; does it increase useful findings
without unacceptable false positives; and what should be removed or
deferred if the evidence turns out weak. Signals such as churn, cross-PR
overlap, cross-repo relation, repository history, or missing context must
never automatically become defects -- they may only ever strengthen or
deepen scrutiny of a candidate that already has independent evidence.

## Completed

| Milestone | Name |
|---|---|
| J | Change Intelligence |
| K | Contract & Blast Radius Intelligence |
| L | Intent Verification |
| M | Test Intelligence |
| N | Historical Regression Memory |
| O | Repository Learnings |
| P | Trajectory Intelligence |
| Q | Cross-PR Intelligence |
| R | Cross-Repo Intelligence |
| S | Executable Verification + Review Effectiveness Benchmark (S1-S5) |
| S6 | Production Execution Enablement |
| T | Agent Handoff / MCP + Fix Verification Loop |
| U | OpenAI Provider + Model Router |
| V | Merge Readiness / Decision Layer |
| W | Cloud Foundation (private `patchfrog-cloud` repository) |
| X | Cloud Private Beta (private `patchfrog-cloud` repository) |

Each is a deterministic, non-LLM evidence layer over the repository/PR graph.
See `docs/agent-orchestration.md`'s "Intelligence layer ownership" table and
each package's own `docs/<package>.md` for what it owns and its exact scope
decisions.

Milestone S (S1 secure execution foundation, S2 existing-targeted-test
verification, S3 runtime evidence integration, S4 review-effectiveness
benchmark v1, S5 verification telemetry) shipped, then underwent a security
correction round that replaced its original process/network/PID-only
isolation with real `bwrap`-based filesystem confinement after an empirical
escape was found and fixed -- see
`validation/executable_verification/latest-summary.md` sections 3 and 22.

Milestone T (T1-T3) shipped in full -- see `docs/agent-handoff.md` and
`validation/agent_handoff/latest-summary.md` for the full audit,
architecture, and exactly what evidence is (and is not) exposed, plus two
post-implementation security correction rounds (false `FIXED` and false
`STILL_PRESENT` paths in T3's fix-verification classifier) and a final
acceptance correction (no evidence must never invoke the LLM fallback).

**U — OpenAI Provider + Model Router**

OpenAI support is deliberately sequenced *after* Executable Verification and
Agent Handoff: a third provider is less valuable right now than stronger
empirical verification and agent interoperability.

- U1 — OpenAI provider adapter (`patchfrog.review.providers.openai_provider`)
  — implemented: official SDK, Responses API, same `LLMProvider` contract as
  Anthropic/Gemini.
- U2 — model capability registry (`patchfrog.routing.capabilities`) —
  implemented, deliberately minimal (structured-output capability only, no
  marketing-claim cost/reasoning tiers).
- U3 — deterministic routing (`patchfrog.routing.router.ModelRouter`) —
  implemented: operator-policy-bounded, once per review run, bounded
  one-hop fallback.
- U4 — reviewer/critic model-family diversity — implemented: auto-diversity
  when more than one provider is configured, never required with one.
- U5 — disagreement handling / independent verifier — **not implemented**
  this round; a materially larger, distinct problem from U4's family
  diversity. See `docs/model-routing.md`'s own "Not implemented this
  round" section.

See `docs/model-routing.md` for the full architecture and
`validation/model_router_merge_readiness/latest-summary.md` for the audit.

**V — Merge Readiness / Decision Layer**

Goal: derive explainable outcomes (`READY` / `BLOCKED` /
`HUMAN_REVIEW_REQUIRED`) from evidence -- unresolved verified findings,
same-exact-head fix verification, review completeness. **Never** an
arbitrary numeric risk score; every decision is explainable back to the
specific evidence that produced it. Implemented: `patchfrog.merge_readiness`
domain/service (deterministic, recomputed fresh per exact head, never
persisted/cached), the read-only MCP `get_merge_readiness` tool. The
GitHub PR Review summary surface is deliberately deferred (see
`docs/merge-readiness.md`'s own "Exposure surface" section for why). J-R
Intelligence deliberately never blocks or escalates on its own -- see
`docs/merge-readiness.md`.

Milestones W (Cloud Foundation) and X (Cloud Private Beta) shipped in
the private `kadireren7/patchfrog-cloud` repository -- accounts,
workspaces, official GitHub App installation ownership, repository
enrollment, an idempotent hosted review-job pipeline consuming this
engine as a pinned dependency (never forking or duplicating it), usage/
quota, and a minimal dashboard. Neither is publicly released or
announced yet -- private beta only. See `docs/product-boundary.md`'s
"Repository split and the engine/Cloud relationship" section.

## Current — Product Intelligence

Under development on `feat/y-z-learning-governance`, open for review,
not yet merged to `main`.

**Y — Repository & Organization Learning**

Implemented: a durable, explainable snapshot layer
(`patchfrog.learning_records`) over evidence already persisted by
Milestones N/O/Phase 9 -- `useful_finding_pattern` (a snapshot of
Milestone O's own repeated-trusted-surface detection) and
`noise_suppression` (repeated, uncontradicted false-positive feedback on
one exact surface), each with simple `candidate`/`established`/`retired`
maturity, never a fake percentage. Organization-level aggregation is a
generic engine primitive scoped only by an explicit `repository_ids`
tuple the caller (Cloud) supplies -- the public engine still has no
workspace concept of its own. Personalization effects
(`patchfrog.learning_records.personalization`) are advisory/ordering-
only by construction and fully tested, but **not yet wired into**
`patchfrog/review/service.py`'s live candidate scheduling -- a
deliberate, documented scope cut (see
`validation/org_learning_governance/latest-summary.md` section 4.3 and
`docs/repository-learning.md`). User preference never redefines
objective correctness: nothing here can suppress a strong deterministic
or security finding.

**Z — Policy / Governance**

Implemented: one generic, deterministic policy evaluation system
(`patchfrog.governance`) -- platform/organization/repository precedence
that can only ever tighten, typed reason codes, integration with Merge
Readiness (tightening-only wrapper, never a second readiness engine),
the Model Router (an optional provider allowlist), and Executable
Verification (policy-required verification, S/S6's sandbox semantics
untouched). Cloud owns policy definition, assignment, audit, and UI; the
public engine owns evaluation only. RBAC/SSO/SAML/retention controls
remain out of scope for this round -- see `docs/governance-policy.md`.

## Then — Advanced Verification

**AA — Multi-Repository System Intelligence**

Beyond Milestone R's explicit, manual, operator-registered relationship
foundation. Potential future evidence sources: OpenAPI, protobuf, GraphQL
schemas, event schemas, generated clients, package manifests, versioned
contracts. Goal example: an API producer removes a field while an
explicitly known consumer still relies on it. Not implemented during S.

**AB — Verification Lab**

Advanced evolution of Executable Verification. Potential future
capabilities: generated targeted tests, deterministic reproductions,
differential base-vs-head execution, mutation-inspired verification.
Example: the same reproduction passes on BASE and fails on HEAD -- stronger
runtime evidence than a single-head run. Not implemented during S unless
separately approved.

**AC — Autonomous Fix Verification**

A coding agent proposes a patch; PatchFrog independently verifies it against
the exact new head using static evidence, contract evidence, tests, runtime
verification, and whether the original finding remains. PatchFrog's
long-term role is verifier/judge, not an uncontrolled patch generator.

## Long-term

**AD — Review Operating System**

The long-term product thesis in full: AI coding agents write and change
code; PatchFrog independently understands the change, checks contracts,
checks intent, checks tests, checks repository history, checks trajectory,
checks concurrent PRs, checks explicit cross-repo dependencies, executes
targeted verification, verifies agent fixes, and decides merge readiness.

**This is direction, not current product capability.** Never advertise an
unfinished roadmap item (anything at or after the "Current" milestone above)
as available today.

## Test counts are not review quality

Implementation tests prove engine correctness. They do **not** prove
PatchFrog is a good reviewer -- a rising implementation-test count is a
different claim from rising review effectiveness (finding real bugs, not
raising false positives). Never treat "N tests passing" as evidence of
review quality; that claim needs its own, separate evaluation. The
north-star metrics for review quality are things like known-bug recall,
clean-PR precision, false positives per clean PR, critic save rate, and
execution-confirmed findings -- not comments produced, agent count, or
intelligence-layer count. `validation/review_effectiveness/` (introduced
in Milestone S) exists to measure review effectiveness, separately from
the engine-correctness corpus.
