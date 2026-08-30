# Agent Orchestration v1

`patchfrog/review/agents/`, `patchfrog/review/orchestration.py`, and
`patchfrog/review/critic_selection.py` turn PatchFrog's single
general-purpose reviewer + critic pipeline into a controlled cooperative
review with explicit specialist roles, shared evidence state,
deterministic aggregation, selective escalation, and strict call/token
budgets. This document explains that architecture. It does not
introduce a new phase number -- it extends Phase 5 (the AI Reviewer).

**"The model may propose. PatchFrog decides what survives."** That
principle, unchanged since Phase 5, is exactly as true after this
milestone: every specialist's output still passes through deterministic
validation, deterministic dedup/contradiction rules, and deterministic
confidence aggregation before anything is ever persisted as an accepted
finding, let alone published.

## Specialist roles (v1: exactly two)

- **Correctness** -- functional correctness, control/data-flow mistakes,
  broken contracts, null/state/lifetime/resource errors, concurrency
  correctness where evidence supports it, behavioral regressions, API
  misuse affecting correctness.
- **Security** -- injection, authentication/authorization,
  trust-boundary violations, secret handling, unsafe input/path/process/
  network behavior, memory-safety issues with a real security
  consequence, realistic exploit/impact paths.

Both roles are declared in `patchfrog.review.agents.roles.AgentRole` --
a typed enum, never a string parsed out of a prompt or model output.
Every AI-generated proposal is attributable to the role that created it,
end to end: in memory (`AgentProposal.role`), in the database
(`ai_finding_proposals.agent_role`, `ai_findings.agent_role`), and in
evaluation results (`PredictedFinding.agent_role`).

v1 is deliberately narrow. It does **not** include a style agent,
documentation agent, performance agent, test agent, architect agent, or
free-form planner agent. Those can come later if evidence justifies
them -- this milestone is scoped to two roles precisely so evaluation
can measure "Agent Orchestration alone" cleanly.

Both roles are **provider-neutral**: the same operator-selected provider
and model (see `docs/deployment.md`'s "Provider/model selection"
section -- this remains entirely unchanged, unavailable to repository
configuration) serve both roles, with different role-scoped prompts.
`AgentOrchestrator` accepts a `Mapping[AgentRole, LLMProvider]`, so a
future milestone could route roles to different models without any
repository-controlled field ever needing to exist for it -- v1 always
maps both roles to the identical provider instance.

## No free-form agent chat

Agents never talk to each other. There is no message bus, no shared
scratchpad, no multi-turn debate. Each specialist call is a single,
independent, structured-output request against the exact same shared
evidence (see below); the only thing that flows *between* agents is
already-validated, already-typed state that PatchFrog itself
constructs and interprets -- never raw model output passed as
"instructions" from one agent to another. The critic, when it needs to
resolve a cross-role contradiction, is shown both proposals' *validated
claims and evidence* as clearly delimited data, exactly like repository
content -- never as something to obey.

## Shared evidence package

For one candidate, context is built **exactly once**, using the Context
Engine (`patchfrog.context.service.ContextService`). As of Milestone E
(adaptive multi-hop context, see `docs/context-engine.md`), real reviews
build that context via the Context Engine's deterministic, bounded
adaptive mode -- 1-hop first, expanded to depth 2 only when a structural
signal justifies it -- rather than a fixed 1-hop-only retrieval. The
Context Engine's own ranking, budgeting, and cycle/cost bounds are
entirely its concern; this milestone's invariant is unaffected either
way: whatever the Context Engine decides to include, it decides **once**
per candidate, before any specialist agent runs. The result is wrapped
in a typed, immutable `CandidateEvidencePackage`
(`patchfrog.review.agents.evidence`): the candidate, the exact diff
excerpt, the exact context text actually sent, the static-finding
summaries attached to the candidate, and the exact set of file paths the
model was shown. Both specialist agents receive this identical package
-- adaptive expansion never runs per-role, and Security never receives
deeper context than Correctness.
This is what makes agent outputs comparable, reproducible, cost-bounded,
and independently auditable -- no agent ever rebuilds its own context.

## Typed proposal flow

Each specialist call's structured response is parsed into
`AIReviewFinding`s (unchanged schema and shape from before this
milestone -- both roles still use `REVIEW_RESPONSE_SCHEMA`), each of
which independently passes through the exact same deterministic
validation gate that has always existed
(`patchfrog.review.validation.parse_and_validate_response`): location
bounds, scope (only files actually shown), non-empty
identification/reasoning, and verbatim evidence-quote matching against
the exact context sent. A finding from the Security role is validated
exactly as strictly as one from Correctness -- there is no relaxed path
for either.

A validated (or rejected) finding becomes an `AgentProposal`
(`patchfrog.review.agents.proposal`) -- role, the `ValidatedFinding`,
an optional critic verdict, per-call token usage, and an optional
suppression reason set later by cross-role handling. This replaces the
pre-orchestration assumption of "one reviewer response per candidate."

## Deterministic role selection

`patchfrog.review.agents.selection.AgentSelectionPolicy` decides which
roles actually run for a candidate -- a pure function of already-known
structural data (the candidate, its attached static findings), **never**
an LLM "router." Correctness is always selected. Security is selected
when a static finding on the candidate is already in the security
category, or the candidate's file/symbol name matches a fixed,
explainable security-sensitive-naming heuristic, or (v1's honest
fallback, since no more reliable signal exists yet) conservatively by
default. Every selection carries an explicit, audited reason
(`AgentSelectionReason`) -- this is the seam a future milestone can
tighten without touching the orchestrator itself.

## Selective critic verification

`patchfrog.review.critic_selection.CriticSelectionPolicy` replaces
"critique every valid proposal unconditionally" with a fixed,
explainable rule set. It only ever *skips* the critic in one case: a
proposal that is HIGH confidence, non-security, below HIGH severity,
already corroborated by an independent static finding, and does not
overlap another role's proposal for the same candidate. Everything else
-- HIGH/CRITICAL severity, security category, LOW/MEDIUM confidence,
cross-role overlap, or anything not statically corroborated -- is still
critiqued, preserving today's effective safety for anything remotely
risky while cutting real, explainable cost on the lowest-risk case.

## Cross-role dedup and contradiction handling

`patchfrog.review.agents.cross_role` runs once per candidate, after both
roles' proposals have independently passed validation:

- **Same root cause** (overlapping location, and either the same
  category or verbatim-shared evidence) collapses to one surviving
  proposal, deterministically: Security's categorization wins when the
  Security-role proposal is itself genuinely security-categorized and
  evidence overlaps (the canonical example: Correctness says "shell
  argument construction is incorrect", Security says "untrusted value
  reaches shell invocation" -- same bug, Security's framing survives);
  otherwise higher severity, then higher confidence, then a stable
  tie-break. No provider call ever decides this.
- **Genuinely different bugs** at the same location (no shared
  evidence, different category) are never merged -- both survive.
- **Contradictions** -- two proposals sharing evidence but making
  lexically opposite safety claims (e.g. one says a value is
  unsanitized, the other says a sanitizer guarantees safety) -- are
  flagged and force critique on both sides regardless of
  `CriticSelectionPolicy`. The critic is shown both claims plus the
  shared evidence explicitly labeled as a second specialist's
  conflicting claim (never as an instruction) and decides
  accept/reject/downgrade for each independently. If the critic still
  can't reject at least one side (only one may survive; two or more
  still-standing members means the critic didn't confidently resolve
  it), PatchFrog suppresses every member of that group
  (`ProposalStatus.SUPPRESSED_CONTRADICTION`) rather than publish
  contradictory comments about the same code.

This is a narrow, deterministic mechanism, not a debate system --
detection and merge-preference are both fixed rules over
already-validated typed data, never a provider call.

## Call and token budgeting

For each candidate, both selected roles' prompts are estimated together
and reserved **atomically** against the run's existing
`max_total_input_tokens` budget (`ReviewConfig`, unchanged repository
field, unchanged meaning): a candidate whose combined two-role estimate
would exceed the remaining budget is skipped entirely -- never
partially reviewed by only one role. `max_candidates`,
`max_concurrent_requests`, and `max_output_tokens_per_candidate` all
keep their pre-orchestration meaning; the two roles' calls for one
candidate may run concurrently, but everything about the run stays
bounded by the same repository-controlled behavior config as before.
`ReviewRunSummary`/`ReviewRunModel` now additionally break `reviewer`
token usage down per role (`correctness_input_tokens`,
`security_input_tokens`, etc. -- nullable-safe on historical rows) --
the pre-existing totals keep their exact prior meaning.

No new repository-controlled field was added for any of this, and
`.patchfrog.yml` still cannot select or influence which AI provider or
model runs (see `docs/deployment.md`) -- that trust/cost boundary,
established in the prior milestone, is completely unaffected here.

## Failure semantics

A single specialist role failing (a transient or fatal provider error)
never fails the whole candidate if the other role's result is usable --
the run proceeds with whatever validated, useful work exists. Only when
**every** selected role fails for a candidate is that candidate marked
failed. A critic failure falls back to no-critic aggregation, exactly as
before -- except for a proposal inside an unresolved contradiction
group, where a missing verdict is treated the same as "not confidently
resolved" and the group is suppressed rather than defaulting to accept.

## Context Engine depth (superseded)

At the time Agent Orchestration v1 shipped, the Context Engine was fixed
at 1-hop retrieval and this section said so. Milestone E (see
`docs/context-engine.md`) has since added deterministic, bounded
adaptive expansion to depth 2, now the default for real reviews -- still
capped at depth 2, still fully deterministic, still built once per
candidate and shared identically by both specialist roles (see "Shared
evidence package" above). This did not require any change to
orchestration itself; the two milestones were deliberately sequenced so
each could be evaluated independently before the other existed.

## What did not change (as of this document's own milestone)

- Candidate generation (`patchfrog.review.candidates`) -- untouched by
  Agent Orchestration itself (later extended by Milestone E for
  adaptive expansion -- see `docs/context-engine.md`).
- The Context Engine's ranking/scoring/dedup/budgeting -- untouched by
  Agent Orchestration itself.
- The GitHub comment format (`patchfrog.publishing.body`) -- unchanged;
  `agent_role` is persisted for internal audit only and is structurally
  absent from `PublishableFinding`, so it can never appear in a rendered
  comment. PatchFrog still presents one coherent review, never "Security
  Agent #2 says...".
- Repository-controlled provider/model selection -- still impossible;
  unrelated to and unaffected by this milestone.
- The evaluation harness's oracle/`FakeLLMProvider`-scripted modes,
  static-only mode, critic ablation, and context ablation -- all
  preserved. `FakeLLMProvider`'s existing `response_factory` hook, now
  paired with `patchfrog.review.providers.fake.route_by_schema_name`,
  lets a test script each role's (and the critic's) response
  deterministically by `schema_name`
  (`"review_response:correctness"`/`"review_response:security"`/
  `"critic_verdict"`) rather than depending on call order -- required
  once two specialist calls for one candidate can be in flight at once.

## Quality + Cost Guard (superseded default execution shape)

At the time Agent Orchestration v1 shipped, every candidate got
identical treatment: both roles, the same context/output budget
fraction, the same critic selectivity, the same retry allowance. A
later milestone (see `docs/quality-cost-guard.md`) introduced
deterministic per-candidate effort tiering (LIGHT/STANDARD/DEEP) on top
of this architecture -- tier can reduce which roles run, the context/
output budget, critic strictness, and retry allowance for a candidate,
but never changes anything documented above about *how* a role's
proposal is validated, deduplicated, or resolved against a contradiction,
and never touches provider/model selection. Role selection
(`AgentSelectionPolicy`) and selective critic verification
(`CriticSelectionPolicy`) described above are composed by, not replaced
by, that later tiering layer.

## Versioning

`REVIEW_ENGINE_VERSION`, `REVIEW_PROMPT_VERSION`, and
`REVIEW_POLICY_VERSION` (`patchfrog.review.config`) were all bumped for
this milestone -- the single-reviewer system prompt was replaced by two
role-scoped prompts (prompt version), new acceptance-affecting policies
were introduced (critic selection, cross-role dedup/contradiction --
policy version), and the execution engine itself materially changed
(one call per candidate became deterministic role selection plus
concurrent specialist calls plus cross-role handling -- engine version).
All three fold into `ReviewModelIdentity`'s fingerprint (and, through it,
`patchfrog.review_memory.config.compute_memory_compatibility_fingerprint`),
so a run canonicalized under the old single-reviewer engine can never be
silently reused as if it already went through orchestration.
`CONFIG_SCHEMA_VERSION` (repository-controlled `.patchfrog.yml`
semantics) is **unchanged** -- this milestone added no new repository
field.
