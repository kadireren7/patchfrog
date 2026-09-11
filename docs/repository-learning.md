# Repository & Organization Learning (Milestone Y)

## What this is, precisely

A **durable, explainable snapshot layer** over evidence PatchFrog
already persists -- never a second, independent detector, never a
"memory blob." Two learning types, both requiring repeated, independent
evidence (never a single event):

- **`useful_finding_pattern`** -- a repeated, trusted (`fixed`/`useful`)
  signal on one exact `(file_path, qualified_name, category)` surface.
  This is a durable *snapshot* of Milestone O's own
  (`patchfrog.repository_learnings`) already-derived output -- Y adds no
  new "is this useful" logic of its own.
- **`noise_suppression`** -- a repeated, uncontradicted `false-positive`
  signal on one exact surface (see
  `patchfrog.learning_records.queries.fetch_repeated_noise_feedback`).

See `patchfrog/learning_records/domain.py` for the full domain model and
`validation/org_learning_governance/latest-summary.md` for the audit.

## Maturity

Simple categorical maturity, never a fake percentage:

- **`candidate`** -- at least 2 independent (distinct finding, distinct
  review run) occurrences.
- **`established`** -- at least 3.
- **`retired`** -- a later recomputation found the surface's supporting
  evidence contradicted (e.g. a noise-suppression surface that later
  received a `useful`/`fixed` signal). Never deleted -- the row keeps
  its last-known evidence, only maturity and `retired_reason` change.

## What a learning can and cannot do

**Allowed** (advisory/ordering only -- see
`patchfrog/learning_records/personalization.py`):

- An `established` `useful_finding_pattern` candidate is scheduled
  earlier in a review run (the same scheduling-order-only mechanism
  Trajectory/Cross-PR/Cross-Repo Intelligence already use).
- A `noise_suppression` learning adds one bounded, factual sentence of
  history to the reviewer/critic's context for a matching candidate --
  it never removes the candidate, never changes its severity, never
  skips the provider call.

**Never allowed, by construction** (no function in this package accepts
or can produce any of the following):

- Inventing a finding.
- Overriding deterministic evidence or Merge Readiness.
- Disabling a security check.
- Selecting a provider/model.
- Auto-merging or auto-approving anything.

A `SECURITY`-category noise-suppression learning is additionally
governed: see `docs/governance-policy.md`'s Z16 section --
`security_suppression_forbidden` (on by the platform default) means it
is never applied to a security finding regardless of how established it
is.

## Organization-level learning (Y7/Y8)

The public engine has **no concept of "organization" or "workspace"** --
that boundary exists only in the private Cloud repository. Organization
aggregation (`patchfrog.learning_records.org_aggregation`) is therefore
a generic primitive: it takes an **explicit** `repository_ids` tuple
supplied by the caller and never infers tenant scope itself. It requires
the same pattern to be independently `established` in at least 2 of the
given repositories -- a single repository's own learning, however
strong, never becomes an "organization" learning on its own. When an
explicit, operator-registered Cross-Repo Intelligence relation
(`patchfrog.persistence.models.cross_repo.RepositoryRelationModel`)
exists between two contributing repositories, the result is tagged
`cross_repo_related=True` -- informational only, never proof of
breakage.

## Background recomputation (Y12)

`patchfrog.learning_records.service.recompute_repository_learnings` is
idempotent (re-running it produces the same persisted state, never a
growing duplicate history) and bounded
(`MAX_RECORDS_PER_RECOMPUTATION`). `apps/worker/tasks/recompute_repository_learnings.py`
exposes it as a Celery task (`patchfrog.recompute_repository_learnings`)
-- deliberately **not** wired onto the PR-review critical path; an
operator or PatchFrog Cloud triggers it directly (e.g. on a schedule, or
after a batch of reviews).

## Explainability (Y9)

Every persisted record answers "what/why/how many/when/scope/maturity"
via `RepositoryLearningRecord.explain()` -- see the read-only MCP tool
`list_repository_learnings` (`patchfrog/mcp/server.py`) and PatchFrog
Cloud's own "Learnings" dashboard views.

## What is deliberately not built this round

Wiring `patchfrog.learning_records.personalization`'s functions into
`patchfrog/review/service.py`'s live candidate-scheduling loop -- the
functions are complete and fully tested on their own, but integrating
into that specific, extremely dense, heavily-tested file is a
deliberate, documented follow-up (see the audit's section 4.3), not
done in this milestone.
