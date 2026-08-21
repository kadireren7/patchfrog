"""Regression detection between two evaluation runs.

Operates on plain JSON dicts (the exact shape
:mod:`patchfrog.evaluation.reporting` produces) rather than reconstructing
full typed dataclasses -- a deliberate simplification: regression
comparison only ever needs specific numeric fields, never full per-case
prediction detail, so a lighter dict-based comparison avoids writing (and
maintaining) a second full deserializer for something
:func:`json.dumps`/:func:`json.loads` already round-trips correctly.

Core philosophy (Phase 8 spec section 28): **precision over recall**. A
small recall increase never justifies a large false-positive increase,
so precision/clean-case/hallucination/duplicate/unsafe-carry-forward
thresholds are strict by default while recall is allowed a much looser
regression budget.

Two runs are only ever compared if their
:class:`~patchfrog.evaluation.domain.EvaluationIdentity` is compatible
(see :func:`identity_compatible`) -- an incompatible comparison (e.g.
different benchmark version, different mode, different provider) always
refuses outright rather than silently producing a misleading verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RegressionThresholds:
    """All configurable -- see the Phase 8 spec's own suggested defaults."""

    max_precision_drop: float = 0.03
    max_clean_pass_rate_drop: float = 0.05
    max_unsupported_increase: int = 0
    max_duplicate_rate_increase: float = 0.02
    max_unsafe_carry_forward: int = 0
    #: Looser: recall may regress by up to this much before it's treated
    #: as a failure -- precision is the priority, not recall.
    max_recall_drop: float = 0.10


#: The module-level default instance -- used as :func:`compare`'s default
#: argument instead of constructing one in the signature (ruff B008).
DEFAULT_REGRESSION_THRESHOLDS = RegressionThresholds()


_IDENTITY_KEYS_MUST_MATCH = (
    "evaluation_benchmark_version",
    "evaluation_engine_version",
    "review_engine_version",
    "review_prompt_version",
    "review_policy_version",
    "incremental_review_engine_version",
    "review_memory_version",
    "mode",
)


def identity_compatible(baseline_identity: dict[str, Any], current_identity: dict[str, Any]) -> tuple[bool, str | None]:
    for key in _IDENTITY_KEYS_MUST_MATCH:
        if baseline_identity.get(key) != current_identity.get(key):
            return False, f"identity field {key!r} differs: baseline={baseline_identity.get(key)!r} current={current_identity.get(key)!r}"
    return True, None


@dataclass(frozen=True, slots=True)
class RegressionCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RegressionVerdict:
    passed: bool
    identity_compatible: bool
    identity_mismatch_detail: str | None
    checks: tuple[RegressionCheck, ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        """0 = within thresholds. 1 = quality regression. 2 = the two
        runs' identities aren't even comparable -- a distinct code so CI
        can tell "the code got worse" apart from "this comparison was
        never valid to begin with" (see Phase 8 spec section 48)."""

        if not self.identity_compatible:
            return 2
        return 0 if self.passed else 1


def compare(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    thresholds: RegressionThresholds = DEFAULT_REGRESSION_THRESHOLDS,
) -> RegressionVerdict:
    compatible, mismatch = identity_compatible(baseline["identity"], current["identity"])
    if not compatible:
        return RegressionVerdict(passed=False, identity_compatible=False, identity_mismatch_detail=mismatch)

    checks: list[RegressionCheck] = []
    b_metrics = baseline["metrics"]
    c_metrics = current["metrics"]

    b_precision = b_metrics["overall"]["scores"]["precision"]
    c_precision = c_metrics["overall"]["scores"]["precision"]
    precision_drop = b_precision - c_precision
    checks.append(
        RegressionCheck(
            name="precision", passed=precision_drop <= thresholds.max_precision_drop,
            detail=f"baseline={b_precision:.4f} current={c_precision:.4f} drop={precision_drop:.4f} "
            f"(threshold {thresholds.max_precision_drop:.4f})",
        )
    )

    b_recall = b_metrics["overall"]["scores"]["recall"]
    c_recall = c_metrics["overall"]["scores"]["recall"]
    recall_drop = b_recall - c_recall
    checks.append(
        RegressionCheck(
            name="recall", passed=recall_drop <= thresholds.max_recall_drop,
            detail=f"baseline={b_recall:.4f} current={c_recall:.4f} drop={recall_drop:.4f} "
            f"(threshold {thresholds.max_recall_drop:.4f}, looser by design -- precision > recall)",
        )
    )

    b_clean = b_metrics["clean"]["pass_rate"]
    c_clean = c_metrics["clean"]["pass_rate"]
    clean_drop = b_clean - c_clean
    checks.append(
        RegressionCheck(
            name="clean_case_pass_rate", passed=clean_drop <= thresholds.max_clean_pass_rate_drop,
            detail=f"baseline={b_clean:.4f} current={c_clean:.4f} drop={clean_drop:.4f} "
            f"(threshold {thresholds.max_clean_pass_rate_drop:.4f})",
        )
    )

    b_unsupported = b_metrics["hallucination"]["unsupported_after_validation"]
    c_unsupported = c_metrics["hallucination"]["unsupported_after_validation"]
    unsupported_increase = c_unsupported - b_unsupported
    checks.append(
        RegressionCheck(
            name="unsupported_accepted_findings", passed=unsupported_increase <= thresholds.max_unsupported_increase,
            detail=f"baseline={b_unsupported} current={c_unsupported} increase={unsupported_increase} "
            f"(threshold {thresholds.max_unsupported_increase})",
        )
    )

    b_dup = b_metrics["overall"]["duplicate_rate"]
    c_dup = c_metrics["overall"]["duplicate_rate"]
    dup_increase = c_dup - b_dup
    checks.append(
        RegressionCheck(
            name="duplicate_rate", passed=dup_increase <= thresholds.max_duplicate_rate_increase,
            detail=f"baseline={b_dup:.4f} current={c_dup:.4f} increase={dup_increase:.4f} "
            f"(threshold {thresholds.max_duplicate_rate_increase:.4f})",
        )
    )

    c_incremental = current.get("incremental")
    c_unsafe = c_incremental["unsafe_carry_forward_count"] if c_incremental else 0
    checks.append(
        RegressionCheck(
            name="unsafe_carry_forward", passed=c_unsafe <= thresholds.max_unsafe_carry_forward,
            detail=f"current={c_unsafe} (threshold {thresholds.max_unsafe_carry_forward}; target is always zero)",
        )
    )

    return RegressionVerdict(
        passed=all(c.passed for c in checks), identity_compatible=True, identity_mismatch_detail=None,
        checks=tuple(checks),
    )
