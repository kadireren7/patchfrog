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

Each is a deterministic, non-LLM evidence layer over the repository/PR graph.
See `docs/agent-orchestration.md`'s "Intelligence layer ownership" table and
each package's own `docs/<package>.md` for what it owns and its exact scope
decisions.

## Current — Review Engine

**S — Executable Verification + Review Effectiveness Benchmark**

Conceptual sub-phases (not all necessarily implemented in one PR -- each
milestone's own audit chooses the narrowest safe v1 subset; see that
milestone's own `validation/executable_verification/latest-summary.md` for
which sub-phases actually shipped and which were deferred):

- **S1 — Secure Execution Foundation**: sandbox abstraction, isolation
  guarantees, threat model.
- **S2 — Existing Targeted Test Verification**: the narrowest verification
  primitive (run one already-existing, structurally-related test, never the
  full suite, never a generated test).
- **S3 — Runtime Evidence Integration**: bounded runtime evidence reaches the
  critic/verifier as additional evidence, never a standalone finding.
- **S4 — Review Effectiveness Benchmark v1**: a durable corpus + metrics that
  measure reviewer quality, separate from implementation-test correctness.
- **S5 — Verification Telemetry**: bounded, privacy-safe counts describing
  how much/what kind of verification ran.

## Next — Review Engine

**T — Agent Handoff / MCP**

Goal: hand verified PatchFrog evidence directly to coding agents (Claude
Code, Codex, Cursor, or other MCP-capable tools). PatchFrog remains
verifier/judge, not necessarily the patch author.

- T1 — Finding Handoff Schema
- T2 — MCP Server
- T3 — Fix Verification Loop

**U — OpenAI Provider + Model Router**

OpenAI support is deliberately sequenced *after* Executable Verification and
Agent Handoff: a third provider is less valuable right now than stronger
empirical verification and agent interoperability.

- U1 — OpenAI provider adapter
- U2 — model capability registry
- U3 — deterministic routing
- U4 — reviewer/critic model-family diversity
- U5 — disagreement handling / independent verifier

**V — Merge Readiness / Decision Layer**

Goal: derive explainable outcomes (e.g. `READY` / `BLOCKED` /
`HUMAN_REVIEW_REQUIRED`) from evidence -- unresolved verified findings,
contract issues, test evidence, executable verification, historical
evidence, cross-PR/cross-repo evidence. **Never** an arbitrary numeric risk
score as the primary decision model; every decision must be explainable back
to the specific evidence that produced it.

## Then — Cloud

**W — Cloud Foundation**

- W1 — Private Cloud Control Plane Skeleton
- W2 — Official GitHub App
- W3 — Engine Invocation Boundary
- W4 — Managed Provider Runtime
- W5 — Usage Metering

Future private repository: `kadireren7/patchfrog-cloud` (**not created
yet** -- see `docs/product-boundary.md`'s "Repository split and the
engine/Cloud relationship" section). Cloud must consume the public,
source-available PatchFrog Engine; it must never become a second review
engine.

**X — Cloud Private Beta**

Goal: invite-only users/teams, repository onboarding, installation health,
failed-review recovery, review history, a minimal Cloud dashboard, real
usage observation. Success is not "Cloud deploys" -- success is "real users
use it and findings are useful," tracked via PRs reviewed, useful findings,
false-positive feedback, review latency, provider cost per PR, and
executable-verification cost per PR.

## Then — Product Intelligence

**Y — Repository & Organization Learning**

Possible areas: repository review profile, accepted/rejected patterns,
feedback-calibrated prioritization, architecture conventions, trusted
organization knowledge. User preference must never redefine objective
correctness.

**Z — Policy / Governance**

Possible areas: organization review policies, required checks, security
policy, repository groups, audit trail, retention controls, RBAC, SSO/SAML,
enterprise administration. The public engine may contain generic
policy-evaluation primitives; Cloud-specific administration remains private.

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
