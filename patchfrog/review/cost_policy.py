"""Operator-controlled review cost policy (M4).

Decides the *shape* of provider work for a whole review run from the
deterministic PR-level :class:`~patchfrog.change_risk.ChangeRiskTier`:

- ``NO_AI`` -> zero provider calls, deterministic result only.
- everything else -> one structured single-pass reviewer call per batch
  of candidates (``ReviewStrategy.COST_AWARE``), with sequential,
  reason-carrying specialist escalation only for ELEVATED/HIGH_RISK.

Per-tier provider-call ceilings are applied to the *existing*
:class:`~patchfrog.review.budget.ReviewBudget` ledger (as
``min(repository max_provider_calls, tier budget)``), so reviewer calls,
retries, fallbacks and critic calls all count against one number.

Like provider/model selection, this is operator/deployment policy
(environment settings), never ``.patchfrog.yml``: a repository cannot
buy itself a larger budget. The policy's :meth:`fingerprint` is folded
into canonical review-run identity, so an exact-head rerun is only
reused under the identical policy.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from patchfrog.change_risk import CHANGE_RISK_POLICY_VERSION, ChangeRiskPolicy, ChangeRiskTier

#: Bumped when the cost-aware execution shape changes (batching,
#: escalation rules, tier->context mapping) -- distinct from
#: CHANGE_RISK_POLICY_VERSION (classification) and
#: QUALITY_COST_POLICY_VERSION (per-candidate effort tiering).
REVIEW_COST_POLICY_VERSION = 1


class ReviewStrategy(StrEnum):
    #: M4 default for production entry points: PR-level risk tier,
    #: zero-call NO_AI path, single-pass unified reviewer call, sequential
    #: reason-carrying escalation.
    COST_AWARE = "cost_aware"
    #: The pre-M4 per-candidate Correctness+Security fan-out. Kept as a
    #: selectable operator strategy and as the "before" arm of the cost
    #: benchmark; secondary, not removed.
    SPECIALIST_FANOUT = "specialist_fanout"


class EscalationReason(StrEnum):
    """Why an *additional* specialist provider call was made after the
    single-pass reviewer call. Every extra call carries exactly one."""

    SECURITY_SENSITIVE_CHANGE = "security_sensitive_change"
    STATIC_HIGH_RISK_FINDING = "static_high_risk_finding"
    CI_CONFIG_CHANGE = "ci_config_change"
    DATABASE_MIGRATION = "database_migration"
    PUBLIC_CONTRACT_CHANGE = "public_contract_change"
    CROSS_MODULE_COMPLEXITY = "cross_module_complexity"
    HIGH_RISK_FIRST_PASS_FINDING = "high_risk_first_pass_finding"
    OPERATOR_POLICY = "operator_policy"


#: Total provider-call ceilings per run tier (reviewer + escalation +
#: critic + every retry/fallback). TINY's total is 2 = exactly ONE
#: reviewer call plus ONE verification-only call that is spent only when
#: a returned finding requires critic verification: with a total of 1, a
#: tiny PR's HIGH/security finding could never be verified and the
#: existing safety rule would suppress it -- cost saved by dropping a
#: useful finding, which M4 explicitly rules out. A clean tiny PR costs
#: exactly one call. Operators can still set ``{"tiny": 1}`` for a
#: strict single-call policy (and accept that trade-off).
DEFAULT_TIER_PROVIDER_CALL_BUDGETS: Mapping[ChangeRiskTier, int] = MappingProxyType(
    {
        ChangeRiskTier.NO_AI: 0,
        ChangeRiskTier.TINY: 2,
        ChangeRiskTier.NORMAL: 2,
        ChangeRiskTier.ELEVATED: 3,
        ChangeRiskTier.HIGH_RISK: 5,
    }
)

#: Calls inside each total reserved for critic verification: review-phase
#: work (single pass, escalation, their retries) may use at most
#: ``total - reserve`` calls, so it can never starve a mandatory critic.
#: Verification may additionally use any review-phase calls left unused.
TIER_VERIFICATION_RESERVE: Mapping[ChangeRiskTier, int] = MappingProxyType(
    {
        ChangeRiskTier.NO_AI: 0,
        ChangeRiskTier.TINY: 1,
        ChangeRiskTier.NORMAL: 1,
        ChangeRiskTier.ELEVATED: 1,
        ChangeRiskTier.HIGH_RISK: 2,
    }
)

#: Fraction of the per-candidate context budget each run tier may use.
#: TINY/NORMAL are minimized (changed hunk, containing symbol, direct
#: neighbours); ELEVATED/HIGH_RISK keep the per-candidate effort
#: decision's own fraction (1.0 here = no additional narrowing).
TIER_CONTEXT_FRACTION: Mapping[ChangeRiskTier, float] = MappingProxyType(
    {
        ChangeRiskTier.NO_AI: 0.0,
        ChangeRiskTier.TINY: 0.35,
        ChangeRiskTier.NORMAL: 0.5,
        ChangeRiskTier.ELEVATED: 1.0,
        ChangeRiskTier.HIGH_RISK: 1.0,
    }
)

#: Adaptive (depth-2) context expansion is only allowed where the run
#: tier itself already justifies broader context; TINY/NORMAL expansion
#: would need a reason they, by definition, do not have.
TIER_ALLOWS_CONTEXT_EXPANSION: Mapping[ChangeRiskTier, bool] = MappingProxyType(
    {
        ChangeRiskTier.NO_AI: False,
        ChangeRiskTier.TINY: False,
        ChangeRiskTier.NORMAL: False,
        ChangeRiskTier.ELEVATED: True,
        ChangeRiskTier.HIGH_RISK: True,
    }
)


def parse_tier_budgets(raw: Mapping[str, int] | None) -> Mapping[ChangeRiskTier, int]:
    """Operator overrides merged over the defaults. Unknown tier names
    and negative values are rejected (fail closed at startup, never
    silently ignored)."""

    merged = dict(DEFAULT_TIER_PROVIDER_CALL_BUDGETS)
    for key, value in (raw or {}).items():
        try:
            tier = ChangeRiskTier(key)
        except ValueError as exc:
            raise ValueError(f"unknown risk tier in provider-call budgets: {key!r}") from exc
        if int(value) < 0:
            raise ValueError(f"provider-call budget for {key!r} must be >= 0")
        merged[tier] = int(value)
    if merged[ChangeRiskTier.NO_AI] != 0:
        raise ValueError("the no_ai tier budget must be 0 -- it is the zero-call path by definition")
    return MappingProxyType(merged)


@dataclass(frozen=True, slots=True)
class ReviewCostPolicy:
    strategy: ReviewStrategy = ReviewStrategy.COST_AWARE
    tier_provider_call_budgets: Mapping[ChangeRiskTier, int] = field(
        default_factory=lambda: DEFAULT_TIER_PROVIDER_CALL_BUDGETS
    )
    risk_policy: ChangeRiskPolicy = field(default_factory=ChangeRiskPolicy)
    #: Upper bound on one single-pass prompt's estimated input tokens; a
    #: larger candidate set is split into several sequential batches
    #: (each one provider call, all counted against the tier budget).
    single_pass_max_input_tokens: int = 24_000

    def provider_call_budget(self, tier: ChangeRiskTier) -> int:
        """Total provider-call ceiling for a run of this tier."""

        return int(self.tier_provider_call_budgets.get(tier, DEFAULT_TIER_PROVIDER_CALL_BUDGETS[tier]))

    def review_call_budget(self, tier: ChangeRiskTier) -> int:
        """Review-phase ceiling (reviewer + escalation + their retries):
        the total minus the verification reserve, but never below one
        call for a tier that allows any call at all."""

        total = self.provider_call_budget(tier)
        if total <= 0:
            return 0
        return max(1, total - TIER_VERIFICATION_RESERVE[tier])

    def fingerprint(self) -> str:
        payload = {
            "version": REVIEW_COST_POLICY_VERSION,
            "change_risk_policy_version": CHANGE_RISK_POLICY_VERSION,
            "strategy": self.strategy.value,
            "tier_budgets": {t.value: self.provider_call_budget(t) for t in ChangeRiskTier},
            "risk_policy": self.risk_policy.fingerprint_payload(),
            "single_pass_max_input_tokens": self.single_pass_max_input_tokens,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @classmethod
    def from_settings(cls, settings: object) -> ReviewCostPolicy:
        """Build from :class:`~patchfrog.config.settings.Settings` (typed
        as ``object`` to keep this module free of a settings import)."""

        strategy = ReviewStrategy(getattr(settings, "review_strategy", ReviewStrategy.COST_AWARE.value))
        budgets = parse_tier_budgets(getattr(settings, "risk_tier_max_provider_calls", None))
        exclude = bool(getattr(settings, "review_exclude_generated_and_vendor", True))
        return cls(
            strategy=strategy,
            tier_provider_call_budgets=budgets,
            risk_policy=ChangeRiskPolicy(exclude_generated_and_vendor=exclude),
        )


@dataclass(frozen=True, slots=True)
class ReviewCostTelemetry:
    """Run-level M4 cost facts persisted on ``review_runs`` -- identities,
    counts and reasons only."""

    review_strategy: str
    risk_tier: str | None = None
    risk_signals: tuple[str, ...] = ()
    no_ai_reason: str | None = None
    escalation_reasons: tuple[str, ...] = ()
    context_initial_tokens: int = 0
    context_expanded_tokens: int = 0
    context_expansion_reasons: tuple[str, ...] = ()
    cost_policy_fingerprint: str | None = None
    forced: bool = False


__all__ = [
    "DEFAULT_TIER_PROVIDER_CALL_BUDGETS",
    "REVIEW_COST_POLICY_VERSION",
    "TIER_ALLOWS_CONTEXT_EXPANSION",
    "TIER_CONTEXT_FRACTION",
    "TIER_VERIFICATION_RESERVE",
    "EscalationReason",
    "ReviewCostPolicy",
    "ReviewCostTelemetry",
    "ReviewStrategy",
    "parse_tier_budgets",
]
