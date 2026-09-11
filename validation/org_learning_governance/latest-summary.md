# Combined Milestone Y+Z: Repository/Organization Learning + Enterprise
Policy & Governance -- Pre-Implementation Audit

Written before any code, per the spec's "FIRST -- AUDIT" section.

## 1. What already exists (public engine)

**Milestone O (Repository Learnings, `patchfrog/repository_learnings/`)
already implements most of Y1/Y2/Y3/Y9/Y11 at the repository level**:

- `RepositoryLearning` (domain.py): a repeated (`MIN_SUPPORTING_EVENTS =
  2`), independently-trusted (distinct `finding_id` AND distinct
  `historical_review_run_id`) pattern on one exact `(file_path,
  qualified_name, category)` surface. Only pattern kind implemented:
  `REPEATED_SAME_SURFACE_REGRESSION`.
- **Deliberately never persisted as its own row** -- re-derived live,
  per review run, from Milestone N's already-fetched trusted records
  (`historical_regression_report.trusted_records_considered`). This is
  the single biggest design decision Y must reconcile with: Y asks for
  "durable, explainable... typed learning records," which O's own audit
  explicitly chose *not* to build (see O's `latest-summary.md` section
  6/7: an explicit `RETIRED` lifecycle is "unnecessary... invalidation
  falls out naturally from re-deriving live every run").
- **Enrichment-only, never standalone**: `PotentialRepositoryLearningApplication`
  is mandatory-linked to an existing Milestone N candidate on the exact
  same surface -- O never independently rediscovers historical
  relevance (an earlier internal correction round explicitly fixed a
  version that did).
- Explainable: `RepositoryLearningsReport.repository_learning_story`
  (Jinja-free prose prefix, injected into the Change Story) plus
  `evidence_text_for_candidate` (bounded per-candidate evidence text
  shown to the reviewer agent) and `telemetry.summarize_for_persistence`
  (count-only summary, no raw evidence, persisted onto the review run).
- Versioned: `REPOSITORY_LEARNINGS_VERSION = 1`, independent of
  `HISTORICAL_REGRESSION_MEMORY_VERSION`.

**Milestone N (Historical Regression Memory,
`patchfrog/historical_regression_memory/`)** owns the underlying trust
query `fetch_trusted_historical_records` (`queries.py`): joins
`FeedbackEventModel` (`EXPLICIT_COMMAND` events only) -> `AIFindingModel`
-> `ReviewCandidateModel` (for `qualified_name`) -> `ReviewRunModel` (for
`repository_id`/`commit_sha`), grouped by `finding_id`, requiring
`(fixed_count > 0 OR useful_count > 0) AND false_positive_count = 0 AND
ignore_count = 0`, scoped by a **mandatory** `repository_id` equality
filter, `occurred_at <= as_of` (never a future-leaking read). This exact
join shape is what Y5/Y6's new queries mirror -- inverted for
false-positive-only and useful-only cases respectively (see section 4).

**Feedback system (`patchfrog/feedback/`)** already has the two
query primitives Y5/Y6 need, unused by O today:
`get_negative_feedback_findings`/`get_positive_feedback_findings`
(`queries.py`), returning `FindingFeedbackSummary` per finding via
`is_false_positive_candidate`/`is_high_value_candidate`
(`assessment.py`) -- deterministic rule-based classification, never an
ML score. `FeedbackAssessmentModel` is itself already a recomputable,
versioned (`assessment_version`), idempotent-upsert table
(`recompute_and_persist_all`) -- the exact "recompute deterministically
from evidence, never irreversible mutable state" pattern Y11 asks
learning to follow. **Y's own persistence must reuse this pattern, never
build a second one.**

**Trajectory (P) / Cross-PR (Q) / Cross-Repo (R) Intelligence** each
produce a `*ReviewHint` (`INCREASE_CANDIDATE_PRIORITY` /
`REQUIRE_CRITIC`) consumed at exactly one place in
`patchfrog/review/service.py`'s `_execute_and_persist`: a **candidate
scheduling-order sort** (`candidates = tuple(sorted(candidates, key=lambda
c: not _requires_critic(c)))`, ~line 1001) -- never severity, never
suppression, never a second finding source. **This is the established,
low-risk integration pattern for Y4's personalization effects** -- see
section 6's scope decision.

`R` (Cross-Repo Intelligence) has explicit, operator-registered
`RepositoryRelationModel`/`RepositoryContractKeyModel`
(`patchfrog/persistence/models/cross_repo.py`) -- **never** inferred,
never auto-discovered. Y8 must reuse these exact tables, never build a
second relationship registry.

**Merge Readiness (V, `patchfrog/merge_readiness/`)**:
`MergeReadinessService.evaluate(session, repository_id, pull_request_number)`
returns a `MergeReadinessResult` (`decision`, `reason_codes` tuple,
`review_run_id`, `head_sha`, `finding_ids`, `limitations`), deterministic
over already-persisted evidence, never persisted itself, never cached.
Z5's integration point is a **wrapper**, never a modification to this
service's own decision precedence (see section 5).

**Model Router (U, `patchfrog/routing/router.py`)**:
`ModelRouter.route()` builds a `configured: list[str]` of
credentialed+structured-output-capable provider families
(`SUPPORTED_PROVIDERS` filtered by `has_credentials`/
`supports_structured_output`), then selects reviewer/critic/runtime-
fallback families **only from that list**. This is the exact, single,
safe injection point for Z14's provider allowlist: filtering
`configured` once, at its construction, makes every downstream
selection (primary, critic, runtime fallback) automatically respect an
additional allowlist with zero other code changes -- see section 5.

**`.patchfrog.yml` boundary**: `patchfrog/review/config.py`'s
`OPERATOR_ONLY_REVIEW_FIELDS = ("provider", "model", "critic_model",
"request_timeout_seconds")` -- a repository-supplied `review:` section
containing any of these is rejected (`MalformedReviewConfigError`) at
real-review time, warned-and-stripped otherwise. `ReviewConfig` itself
has no provider/model/policy/governance field at all. **Z3's
"`.patchfrog.yml` must not alter policy" is already true by construction
for provider/model** -- Z's own new `.patchfrog.yml` surface (if any) is
scoped identically: repository review *behavior* only, never anything
governance owns.

**Persistence**: `patchfrog/persistence/models/__init__.py` registers
every table on one `Base`. No table today represents a durable learning
record, a policy definition, or a policy decision.

## 2. What does NOT exist yet (confirmed gaps)

- No durable/persisted learning record of any kind (O is fully
  ephemeral by design).
- No false-positive-repetition query (Y5) or useful-finding-repetition
  query (Y6) -- `get_negative_feedback_findings`/
  `get_positive_feedback_findings` exist but are never grouped by
  structural surface across distinct review runs the way N/O already
  group their own trust query.
- No maturity/lifecycle concept (CANDIDATE/ESTABLISHED/RETIRED) anywhere
  in the codebase.
- No organization/workspace-scoped aggregation anywhere in the public
  engine -- the public engine has **no concept of "workspace" at all**;
  that concept exists only in the private Cloud repo. This is an
  architectural fact, not a gap to fill in the public repo: org-level
  learning aggregation must be *orchestrated* by Cloud (which alone
  knows workspace membership), calling a public-engine primitive that
  takes an **explicit** `repository_ids` list -- never inferring tenant
  scope itself (see the spec's own "TENANT ISOLATION" section).
- No governance/policy system of any kind -- no `PolicyDefinition`, no
  evaluation service, no reason codes, no precedence/merge semantics.
- No provider allowlist parameter on `ModelRouter` (only the existing
  operator `PATCHFROG_REVIEW_PROVIDER`/`PATCHFROG_ROUTER_FALLBACK_PROVIDER`
  single-provider settings).
- No Merge-Readiness-tightening wrapper.
- No MCP read-only governance/learning tools (only `get_merge_readiness`,
  `list_findings`, `get_finding_handoff`, `start_fix_attempt`,
  `get_fix_attempt` exist today, registered via `@self.mcp.tool()` in
  `patchfrog/mcp/server.py`).

## 3. Cloud repo audit (`kadireren7/patchfrog-cloud`, main @ `01a32ac`)

- `WorkspaceModel`/`WorkspaceMembershipModel` (owner/member) already
  give Cloud the tenant boundary the public engine deliberately lacks.
- `RepositoryEnrollmentModel` (workspace_id, github_repository_id,
  enabled) is the existing, authoritative "which repositories does this
  workspace own" mapping -- **this is what Y7's org-learning
  aggregation must scope from**, and what Z7's policy assignment
  (workspace-wide + repository override) must key off.
- `ReviewJobModel` already has an idempotency key and status lifecycle
  -- audit-friendly by construction; Z10's audit log is a **new,
  separate** table (governance changes are not review jobs) but should
  mirror its append-only, typed-reason shape.
- `patchfrog_cloud/engine_integration.py` is the **only** module
  Cloud is allowed to import engine internals from
  (`patchfrog.merge_readiness.service.MergeReadinessService`,
  `patchfrog.routing.router` not yet imported there today). Z5/Z14's
  Cloud-side wiring must extend this module, never bypass it.
- `patchfrog_cloud/quota.py`/`QuotaPolicyModel` already establish the
  "workspace-scoped, admin-adjustable, no billing" pattern Z2's
  PROVIDER/COST category can reuse conceptually (kept separate --
  quota is consumption-bound, policy is behavior-bound).
- No policy, audit, or learning table exists in Cloud today.
- Dashboard (`apps/api/routes/dashboard.py`) has exactly the
  `require_workspace_membership` (fail-closed 404) and
  `WorkspaceRole.OWNER`-gated-action patterns Y13/14 and Z11/12 must
  reuse verbatim -- never a new authorization mechanism.

## 4. Scope decisions (Y)

1. **New, additive persistence, not a rewrite of O.** O's own
   `RepositoryLearning` domain object stays exactly as-is (still
   ephemeral, still `REPEATED_SAME_SURFACE_REGRESSION`-only). A new
   package `patchfrog/learning_records/` adds a **durable snapshot**
   layer: a `RepositoryLearningRecordModel` row per distinct
   `(repository_id, learning_type, surface_key)`, recomputed
   idempotently (upsert, never irreversible mutation) from: (a) O's own
   already-computed `RepositoryLearningsReport` for the
   `REPEATED_REGRESSION_PATTERN` type, and (b) two **new** queries
   mirroring N's `fetch_trusted_historical_records` shape exactly, for
   `NOISE_SUPPRESSION` (repeated `false_positive`-only, Y5) and
   `USEFUL_PATTERN` (repeated `useful`/`fixed`, Y6) types.
2. **Maturity** (Y3): `CANDIDATE` (support_count >= `MIN_SUPPORTING_EVENTS`
   i.e. 2) / `ESTABLISHED` (support_count >= a new, higher
   `ESTABLISHED_SUPPORT_THRESHOLD`, default 3) / `RETIRED` (latest
   recomputation found the surface's supporting evidence contradicted --
   e.g. a `NOISE_SUPPRESSION` learning whose surface later gets a
   `useful`/`fixed` signal). Recomputed fresh each run -- never a manual
   counter that can drift from source evidence.
3. **Personalization (Y4) integration point**: reuses the *exact*
   Trajectory/Cross-PR/Cross-Repo `*ReviewHint` -> scheduling-order-sort
   pattern in `review/service.py` (section 1) -- `INCREASE_CANDIDATE_PRIORITY`
   for `USEFUL_PATTERN` surfaces. Noise suppression is **advisory
   evidence text only** (mirrors O's own `evidence_text_for_candidate`
   pattern exactly: informs the reviewer/critic that this exact surface
   has repeatedly been marked false-positive here, never a hard filter,
   never deleted candidate, never skipped provider call) -- this is a
   deliberate, conservative scope cut: the spec's own acceptance
   criterion ("cannot suppress strong deterministic/security evidence")
   is satisfied *by construction* this way, without needing a second
   suppression code path that could itself become a bug. A harder
   suppression effect (e.g. skipping publication of a LOW-confidence,
   non-security finding matching an `ESTABLISHED` noise learning) is
   documented as a natural, low-risk follow-up, not built this round --
   see the final report's "remaining limitations."
4. **Org-level learning (Y7)**: lives in the public engine as a
   **generic primitive** (`patchfrog/learning_records/org_aggregation.py`)
   that takes an explicit `repository_ids: tuple[UUID, ...]` (never
   infers tenant), requires the pattern to be `ESTABLISHED` in
   `ORG_MIN_REPOS` (default 2) *distinct* repositories among the given
   set before producing an `OrganizationLearning`. Cloud's own worker
   calls this with `repository_ids` resolved from
   `RepositoryEnrollmentRepository.list_for_workspace` -- Cloud owns the
   *scope*, the engine owns the *aggregation logic*.
5. **Cross-repo enrichment (Y8)**: bounded to tagging an
   `OrganizationLearning.cross_repo_related: bool` when an explicit
   `RepositoryRelationModel` edge exists between two of its contributing
   repositories -- informational only, never new proof of breakage,
   preserving R's existing safeguards untouched.
6. **Anti-poisoning (Y2, "TENANT ISOLATION", "ANTI-POISONING RULES")**:
   every evidence source reused here (N's trust query, feedback
   queries) already requires an authenticated `ActorIdentity` and a real
   `finding_id`/`review_run_id`/`repository_id` chain -- there is no
   path from raw repository file content to a `FeedbackEventModel` row.
   The `MIN_SUPPORTING_EVENTS = 2` floor (reused, never lowered) is
   itself the anti-single-event-overfit control the spec asks for.

## 5. Scope decisions (Z)

1. **New package `patchfrog/governance/`**: `domain.py` (categories,
   scopes, typed reason codes, `PolicyRule`/`EffectivePolicy`/
   `PolicyDecision`), `precedence.py` (pure `merge_policies(platform,
   org, repo) -> EffectivePolicy`, tightening-only), `service.py`
   (`PolicyEvaluationService.evaluate(...)`, fully deterministic, zero
   LLM calls, zero I/O beyond what's passed in).
2. **Merge Readiness integration (Z5)**: a new function
   `apply_policy_to_merge_readiness(base_result, policy,
   verification_evidence) -> MergeReadinessResult` in
   `patchfrog/governance/merge_readiness_policy.py` -- calls
   `MergeReadinessService.evaluate` first (unmodified), then may only
   ever tighten (`READY` -> `HUMAN_REVIEW_REQUIRED`, never any direction
   that increases permissiveness), adding policy-specific
   `MergeReadinessReasonCode`-shaped typed reasons. **`V`'s own service
   is never modified.**
3. **Router integration (Z14)**: `ModelRouter.__init__`/`.route()` gains
   an optional `allowed_providers: frozenset[str] | None = None`
   parameter (default `None` = unrestricted, byte-for-byte identical to
   today's behavior for every self-hosted deployment with no governance
   configured). When given, `configured` is filtered by it before any
   selection happens -- reviewer, critic, and runtime fallback all
   inherit the restriction automatically, with no other code change.
4. **Verification integration (Z15)**: a pure function
   `verification_requirement_reason(policy, verification_evidence) ->
   MergeReadinessReasonCode | None` -- policy may require verification
   for a category; if required but absent/unavailable, returns
   `EXECUTABLE_VERIFICATION_REQUIRED`, feeding directly into (2)'s
   tightening -- S/S6's own fail-closed sandbox semantics are never
   touched.
5. **MCP (Z17)**: two new **read-only** tools,
   `get_effective_policy(repository_full_name)` and
   `list_repository_learnings(repository_full_name)`, added the same
   way `get_merge_readiness` was -- no mutation tool of any kind.
6. **Cloud owns definitions/assignment/UI/audit** (`patchfrog_cloud/governance/`):
   `PolicyDefinitionModel` (scope-tagged JSON rule set),
   `PolicyAssignmentModel` (workspace or workspace+repository),
   `PolicyAuditEventModel` (who/what/when/prev-new version). Cloud's
   `engine_integration.py` gains a function that resolves a workspace's
   (+ optional repository override's) `EffectivePolicy` via the
   engine's own `precedence.merge_policies`, then passes
   `allowed_providers`/verification requirements into
   `to_engine_settings`/the review-job call, and the resulting
   `MergeReadinessResult` through `apply_policy_to_merge_readiness`
   before display.
7. **Platform floor**: a single, hard-coded `PLATFORM_POLICY` constant
   in the public engine (e.g. `security HIGH always blocks` --
   already true today via V's own `_BLOCKING_SEVERITIES`) -- Cloud can
   never construct an `EffectivePolicy` weaker than this; `merge_policies`
   enforces it structurally (platform is always the first, most-
   restrictive input, and the merge is provably monotonic-tightening).

## 6. Threat model addressed (see final report's own section)

Every threat in the spec's "SECURITY / THREAT MODEL" section is mapped
to a concrete control in the implementation and covered by at least one
test in the Y/Z test corpora -- summarized in the final report, not
repeated here in full to avoid duplicating the same content twice.

## 7. What is explicitly deferred (not built this round)

- Hard suppression of low-confidence findings by noise learnings
  (advisory-only this round, per section 4.3).
- Any policy template beyond `DEFAULT`/`STRICT_SECURITY`/`AGENT_HEAVY`
  (Z8 says "only if cleanly useful," and even these three are thin
  constructors over the same `EffectivePolicy` shape, not hidden
  semantics).
- Per-path-glob verification requirements beyond a bounded, explicit
  list of path prefixes (no full glob/regex engine).
- Any UI beyond the minimal views Y13/Z11 specify.
- **Newly discovered while implementing Z14, not fixed here**: wiring
  `ModelRouter` (all of Milestone U -- routing, family diversity,
  runtime failover, and this milestone's own `allowed_providers`) into
  the production webhook-driven review task
  (`apps/worker/tasks/review_pull_request.py`), which currently
  constructs providers directly via `provider_factory` and bypasses the
  router entirely (`ModelRouter` is reachable only from `patchfrog.cli`
  today). See `docs/governance-policy.md`'s Z14 section for the full
  account -- a live production trust-boundary rewire, correctly left to
  its own dedicated, carefully-tested follow-up rather than folded into
  this PR.
