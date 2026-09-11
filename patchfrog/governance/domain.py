"""Pure domain model for deterministic policy evaluation (Milestone Z).
No I/O, no LLM, no database session -- exactly like every other
engine-owned domain module.

**Governing rule** (spec Z4): policy evaluation is deterministic over
typed inputs. An LLM may only ever produce *evidence* consumed
elsewhere (a finding, a verification result); it is never asked "does
policy allow this?" and no function in this package accepts a provider/
model/prompt of any kind.

**Precedence is structurally tightening-only** (Z1/Z6): every
:class:`PolicyRule` field is optional (``None`` = "this scope expresses
no opinion"). :func:`patchfrog.governance.precedence.merge_policies`
combines platform -> organization -> repository scopes using, per field,
the single direction that can only ever become *more* restrictive as
more scopes weigh in -- never a generic "last write wins" merge, which
would let a repository silently relax an org/platform constraint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from patchfrog.analysis.domain import FindingCategory, Severity

#: Bumped whenever a policy field's meaning, a reason code, or the
#: precedence-merge direction for any field changes materially.
GOVERNANCE_POLICY_VERSION = 1

#: Explicit, bounded path prefixes only -- never a glob/regex engine
#: (spec Z2: "Start small").
MAX_PATH_PREFIXES = 20


class PolicyCategory(StrEnum):
    """Z2's own six categories -- deliberately no more."""

    REVIEW = "review"
    SECURITY = "security"
    VERIFICATION = "verification"
    MERGE_READINESS = "merge_readiness"
    PROVIDER_COST = "provider_cost"
    LEARNING = "learning"


class PolicyScope(StrEnum):
    """Precedence order: PLATFORM > ORGANIZATION > REPOSITORY in
    *authority* (platform can never be weakened), but REPOSITORY is
    evaluated *last* in the merge so it can only ever add restriction on
    top of what platform/organization already require."""

    PLATFORM = "platform"
    ORGANIZATION = "organization"
    REPOSITORY = "repository"


class PolicyReasonCode(StrEnum):
    """Typed, stable reason codes (Z4) -- never freeform-only logic."""

    REQUIRED_SECURITY_REVIEW_MISSING = "required_security_review_missing"
    EXECUTABLE_VERIFICATION_REQUIRED = "executable_verification_required"
    PROVIDER_NOT_ALLOWED = "provider_not_allowed"
    REVIEW_INCOMPLETE = "review_incomplete"
    ORG_POLICY_REQUIRES_HUMAN_REVIEW = "org_policy_requires_human_review"
    LEARNING_DISABLED_BY_POLICY = "learning_disabled_by_policy"
    SECURITY_SUPPRESSION_FORBIDDEN_BY_POLICY = "security_suppression_forbidden_by_policy"


#: Declaration order = strength order, reused from
#: patchfrog.analysis.domain.Severity -- never redefined here.
SEVERITY_RANK = {s: i for i, s in enumerate(Severity)}


def stricter_severity_floor(a: Severity | None, b: Severity | None) -> Severity | None:
    """The floor that blocks/escalates on the *broadest* set of
    severities -- i.e. the least-severe of the two (MEDIUM is a
    stricter floor than HIGH: it also catches HIGH and CRITICAL)."""

    candidates = [s for s in (a, b) if s is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda s: SEVERITY_RANK[s])


def severity_meets_floor(severity: Severity, floor: Severity) -> bool:
    """Whether ``severity`` is at or above (i.e. at least as severe as)
    ``floor``."""

    return SEVERITY_RANK[severity] <= SEVERITY_RANK[floor]


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One scope's own policy configuration. Every field ``None`` means
    "this scope expresses no opinion" -- never confused with an
    explicit, maximally-permissive value (there is no such value; an
    unset field simply does not participate in the merge for that
    field)."""

    scope: PolicyScope

    # SECURITY
    #: Findings at or above this severity always block Merge Readiness.
    #: Reused, never redefined: matches V's own existing
    #: `_BLOCKING_SEVERITIES` floor (HIGH) as the platform default -- see
    #: precedence.PLATFORM_POLICY.
    security_block_severity_floor: Severity | None = None
    #: Findings at or above this severity (but below the block floor)
    #: require human review rather than auto-READY.
    security_requires_human_review_severity_floor: Severity | None = None
    #: If True at any scope, a NOISE_SUPPRESSION learning may never
    #: suppress/de-prioritize a SECURITY-category candidate (Z16) --
    #: cannot be turned back off by a less-restrictive scope.
    security_suppression_forbidden: bool | None = None

    # VERIFICATION
    verification_required_categories: frozenset[FindingCategory] | None = None
    verification_required_path_prefixes: frozenset[str] | None = None

    # PROVIDER_COST
    #: `None` = no restriction from this scope. The merged effective
    #: value is the intersection of every scope that *does* set this --
    #: a repository can only ever narrow it further, never add a
    #: provider platform/org did not already allow.
    allowed_providers: frozenset[str] | None = None

    # LEARNING
    #: `None` = no opinion (defaults permissive if never set anywhere).
    #: The merged value is `False` if *any* scope sets `False`.
    personalization_enabled: bool | None = None

    def __post_init__(self) -> None:
        if self.verification_required_path_prefixes is not None and len(self.verification_required_path_prefixes) > MAX_PATH_PREFIXES:
            raise ValueError(f"verification_required_path_prefixes exceeds MAX_PATH_PREFIXES={MAX_PATH_PREFIXES}")


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """The single, already-merged, tightening-only combination of
    platform + organization + (optional) repository :class:`PolicyRule`
    objects -- the only shape :mod:`patchfrog.governance.service` and
    the Merge-Readiness/Router/Verification integration points ever
    consume. Never re-merged downstream."""

    security_block_severity_floor: Severity | None
    security_requires_human_review_severity_floor: Severity | None
    security_suppression_forbidden: bool
    verification_required_categories: frozenset[FindingCategory]
    verification_required_path_prefixes: frozenset[str]
    allowed_providers: frozenset[str] | None
    personalization_enabled: bool
    contributing_scopes: tuple[PolicyScope, ...]
    version: int = GOVERNANCE_POLICY_VERSION


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """One deterministic evaluation outcome -- always carries at least
    one typed reason when it constrains anything, never a bare boolean
    with no explanation (Z4)."""

    reason_codes: tuple[PolicyReasonCode, ...]

    @property
    def is_constrained(self) -> bool:
        return len(self.reason_codes) > 0
