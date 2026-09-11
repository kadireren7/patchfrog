# Enterprise Policy & Governance (Milestone Z)

## What this is

A single, generic, deterministic policy evaluation system --
`patchfrog/governance/` -- never a set of ad hoc feature switches spread
across services. **Cloud owns policy definition, assignment, UI, and
audit; the public engine owns evaluation only** (see
`docs/product-boundary.md`).

Governing rule (Z4): policy evaluation is deterministic over typed
inputs. An LLM may only ever produce *evidence* consumed elsewhere (a
finding, a verification result) -- it is never asked "does policy allow
this," and no governance function accepts a provider/model/prompt of
any kind.

## Categories (Z2)

Six, deliberately no more: `REVIEW`, `SECURITY`, `VERIFICATION`,
`MERGE_READINESS`, `PROVIDER_COST`, `LEARNING` -- see
`patchfrog/governance/domain.py`'s `PolicyCategory`.

## Precedence (Z1/Z6)

`patchfrog.governance.precedence.merge_policies(platform, organization,
repository)` is the **only** place scope precedence is decided. Every
`PolicyRule` field is optional (`None` = "this scope expresses no
opinion"); the merge uses, per field, the single direction that can only
ever become *more* restrictive as more scopes weigh in:

| Field | Tightening direction |
|---|---|
| `security_block_severity_floor` / `security_requires_human_review_severity_floor` | least-severe floor wins (catches the most) |
| `security_suppression_forbidden` | `True` if *any* scope sets it |
| `verification_required_categories` / `_path_prefixes` | union (cumulative) |
| `allowed_providers` | intersection (never widened) |
| `personalization_enabled` | `False` if *any* scope sets it |

A repository may only ever tighten; it structurally cannot relax a
stricter organization/platform constraint -- there is no "last write
wins" merge anywhere in this module.

The **platform floor** (`patchfrog.governance.precedence.PLATFORM_POLICY`)
is a single hard-coded constant, matching Merge Readiness's own existing
blocking-severity floor (`HIGH`) exactly -- a deployment with zero
governance configured behaves identically to every release before this
milestone.

## `.patchfrog.yml` boundary (Z3)

Unchanged and already true by construction: `ReviewConfig`
(`patchfrog/review/config.py`) has no provider/model/policy field at
all, and `OPERATOR_ONLY_REVIEW_FIELDS` already rejects an attempt to set
one. A repository's `.patchfrog.yml` was never able to alter policy, and
Z introduces no new repository-controlled surface that could.

## Merge Readiness integration (Z5)

**Never a second Merge Readiness engine.**
`patchfrog.governance.merge_readiness_policy.apply_policy_to_merge_readiness`
wraps an already-computed `MergeReadinessResult` (call
`MergeReadinessService.evaluate` first, exactly as before this
milestone) and may only ever move the decision
`READY -> HUMAN_REVIEW_REQUIRED`. `BLOCKED` is never touched, and
nothing in this function can produce `READY` from a stricter base
decision -- this is a structural property of the function, not just a
convention (see its own tests).

## Model Router integration (Z14)

`ModelRouter.__init__` gained an optional `allowed_providers:
frozenset[str] | None = None` parameter (default `None` = unrestricted,
byte-for-byte identical to pre-Z behavior). When given, it filters the
credentialed-provider list *before* any selection happens, so reviewer,
critic, and runtime fallback all inherit the restriction automatically.
**Credential existence is never itself permission to use a provider** --
the same rule Milestone U's own runtime fallback already established,
now generalized to governance.

**Known gap, discovered while implementing this integration**: the
production, webhook-driven review task
(`apps/worker/tasks/review_pull_request.py`) does not construct a
`ModelRouter` at all today -- it builds providers directly via
`patchfrog.review.provider_factory.build_reviewer_provider`/
`build_critic_provider`, bypassing routing, family diversity, and
runtime failover entirely. `ModelRouter` (all of Milestone U, not just
this milestone's own `allowed_providers` addition) is currently only
reachable from `patchfrog.cli`. This means a governance
`allowed_providers` policy is correctly enforced by the router itself
(see its own test corpus) but is **not yet reachable from a real hosted
review** until that task is rewired to use `route_plan=` instead of
`reviewer_provider=`/`critic_provider=`. Deliberately not fixed in this
PR: it is a live, heavily-guarded production trust boundary (see
`tests/integration/test_review_pull_request_provider_trust_boundary.py`,
which would need its own careful update, not a rushed one) and rewiring
it safely is a separate, substantial change -- tracked as a follow-up,
not silently left undocumented.

## Executable Verification integration (Z15)

`patchfrog.governance.verification_policy.is_verification_required`
only ever answers "does policy require verification for this
candidate" -- S/S6's fail-closed sandbox semantics
(`patchfrog.executable_verification`) are never touched, imported, or
reimplemented. If policy requires verification and it is absent, the
Merge Readiness wrapper above produces `HUMAN_REVIEW_REQUIRED` -- never
a faked `PASS`.

## Learning integration (Z16)

`patchfrog/governance/learning_policy.py`: `personalization_enabled`
(from the effective policy) gates whether
`patchfrog.learning_records.personalization`'s functions may be applied
at all; `security_suppression_forbidden` (on by the platform default)
filters out any `SECURITY`-category `noise_suppression` record before
it ever reaches a personalization effect. Learning can never override
governance -- there is no code path in the other direction.

## MCP (Z17)

Two new **read-only** tools: `get_effective_policy`,
`list_repository_learnings` (`patchfrog/mcp/server.py`). A self-hosted
deployment's MCP server has no Cloud organization/repository policy
layer to read, so `get_effective_policy` always reflects the platform
floor alone there -- PatchFrog Cloud's own dashboard is where an
organization's actual effective policy is surfaced. No mutation tool
(`set_policy`, `disable_security`, `force_ready`, `override_block`)
exists or will be added here.

## Templates (Z8)

`patchfrog/governance/templates.py`: `DEFAULT`, `STRICT_SECURITY`,
`AGENT_HEAVY` -- thin constructors over the exact same `PolicyRule`
shape every hand-authored policy uses. `AGENT_HEAVY` (Z9, for
repositories with a lot of AI-generated code) is always an explicit
operator/workspace-owner choice in Cloud -- PatchFrog never guesses
"AI-generated" from code style.

## What Cloud owns

Policy definitions, workspace/repository assignment (repository may
only tighten), the audit log, and the dashboard UI -- see
`kadireren7/patchfrog-cloud`'s own `docs/policy-operations.md`.
