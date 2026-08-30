"""patchfrog.context.adaptive.AdaptiveExpansionPolicy: deterministic
expansion-trigger signals (A/B/C/E) and directional inference. Every
signal is a pure function of already-known structural data -- no
database, no provider call."""

from __future__ import annotations

import pytest

from patchfrog.analysis.domain import FindingCategory
from patchfrog.context.adaptive import AdaptiveExpansionPolicy
from patchfrog.context.config import MAX_SUPPORTED_ADAPTIVE_DEPTH, AdaptiveContextConfig
from patchfrog.context.domain import ContextCandidate, ContextItemKind, ContextRelationship
from patchfrog.persistence.models.code_index import SymbolModel

_POLICY = AdaptiveExpansionPolicy()


def _candidate(
    relationship: ContextRelationship, *, is_on_changed_line: bool = False, file_path: str = "src/a.py"
) -> ContextCandidate:
    return ContextCandidate(
        kind=ContextItemKind.CALLER if "caller" in relationship.value else ContextItemKind.CALLEE,
        file_path=file_path,
        symbol_id=None,
        symbol_name="x",
        qualified_name="src.a.x",
        start_line=1,
        end_line=5,
        relationship=relationship,
        distance=1 if "direct" in relationship.value else 2,
        reason="test",
        is_on_changed_line=is_on_changed_line,
    )


def _symbol(*, start_line: int = 1, end_line: int = 3) -> SymbolModel:
    return SymbolModel(start_line=start_line, end_line=end_line)


def test_no_signal_no_expansion() -> None:
    decision = _POLICY.decide(
        target_symbol=_symbol(), depth1_candidates=[], depth2_candidates=[], finding_category=None
    )
    assert decision.expand is False
    assert decision.reasons == ()
    assert decision.direction is None


def test_call_chain_continuation_triggers_callee_expansion() -> None:
    """Signal A."""

    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=depth2, finding_category=None
    )
    assert decision.expand is True
    assert "call_chain_continuation" in [r.value for r in decision.reasons]
    assert decision.direction == "callees"


def test_call_chain_continuation_triggers_caller_expansion() -> None:
    depth1 = [_candidate(ContextRelationship.DIRECT_CALLER)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLER)]
    decision = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=depth2, finding_category=None
    )
    assert decision.expand is True
    assert decision.direction == "callers"


def test_both_directions_trigger_bounded_both() -> None:
    depth1 = [_candidate(ContextRelationship.DIRECT_CALLER), _candidate(ContextRelationship.DIRECT_CALLEE)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLER), _candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=depth2, finding_category=None
    )
    assert decision.expand is True
    assert decision.direction == "both"


def test_changed_neighbor_triggers_expansion() -> None:
    """Signal B: a depth-1 neighbor is itself on a changed line, and a
    resolvable second hop exists past it."""

    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE, is_on_changed_line=True)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=depth2, finding_category=None
    )
    assert "changed_neighbor" in [r.value for r in decision.reasons]


def test_changed_neighbor_alone_without_depth2_does_not_trigger() -> None:
    """A changed depth-1 neighbor with no resolvable second hop at all
    must not force an expansion that has nothing to add."""

    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE, is_on_changed_line=True)]
    decision = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=[], finding_category=None
    )
    assert decision.expand is False


@pytest.mark.parametrize(
    "category",
    [
        FindingCategory.MEMORY_SAFETY,
        FindingCategory.RESOURCE_MANAGEMENT,
        FindingCategory.CONCURRENCY,
        FindingCategory.API_MISUSE,
        FindingCategory.SECURITY,
    ],
)
def test_static_category_relevance_triggers_expansion(category: FindingCategory) -> None:
    """Signal C."""

    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=depth2,
        finding_category=category,
    )
    assert "static_category_relevance" in [r.value for r in decision.reasons]


def test_irrelevant_category_does_not_add_category_reason() -> None:
    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=depth2,
        finding_category=FindingCategory.STYLE,
    )
    assert "static_category_relevance" not in [r.value for r in decision.reasons]
    # call_chain_continuation still fires independently.
    assert decision.expand is True


def test_thin_wrapper_triggers_expansion() -> None:
    """Signal E: small target, exactly one direct callee, resolvable
    second hop."""

    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=_symbol(start_line=1, end_line=3), depth1_candidates=depth1, depth2_candidates=depth2,
        finding_category=None,
    )
    assert "thin_wrapper" in [r.value for r in decision.reasons]


def test_thin_wrapper_does_not_trigger_for_large_target() -> None:
    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=_symbol(start_line=1, end_line=200), depth1_candidates=depth1, depth2_candidates=depth2,
        finding_category=None,
    )
    assert "thin_wrapper" not in [r.value for r in decision.reasons]


def test_thin_wrapper_does_not_trigger_with_multiple_callees() -> None:
    depth1 = [
        _candidate(ContextRelationship.DIRECT_CALLEE, file_path="src/a.py"),
        _candidate(ContextRelationship.DIRECT_CALLEE, file_path="src/b.py"),
    ]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=_symbol(start_line=1, end_line=3), depth1_candidates=depth1, depth2_candidates=depth2,
        finding_category=None,
    )
    assert "thin_wrapper" not in [r.value for r in decision.reasons]


def test_decision_is_deterministic_across_repeated_calls() -> None:
    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE, is_on_changed_line=True)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    first = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=depth2,
        finding_category=FindingCategory.SECURITY,
    )
    second = _POLICY.decide(
        target_symbol=_symbol(end_line=20), depth1_candidates=depth1, depth2_candidates=depth2,
        finding_category=FindingCategory.SECURITY,
    )
    assert first == second


def test_none_target_symbol_never_triggers_thin_wrapper() -> None:
    depth1 = [_candidate(ContextRelationship.DIRECT_CALLEE)]
    depth2 = [_candidate(ContextRelationship.TRANSITIVE_CALLEE)]
    decision = _POLICY.decide(
        target_symbol=None, depth1_candidates=depth1, depth2_candidates=depth2, finding_category=None
    )
    assert "thin_wrapper" not in [r.value for r in decision.reasons]
    assert decision.expand is True  # call_chain_continuation still fires


def test_adaptive_context_config_defaults() -> None:
    config = AdaptiveContextConfig()
    assert config.enabled is False
    assert config.initial_depth == 1
    assert config.max_depth == 2


def test_adaptive_context_config_rejects_max_depth_above_v1_cap() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        AdaptiveContextConfig(max_depth=MAX_SUPPORTED_ADAPTIVE_DEPTH + 1)


def test_adaptive_context_config_rejects_initial_depth_other_than_one() -> None:
    with pytest.raises(ValueError, match="initial_depth"):
        AdaptiveContextConfig(initial_depth=2)


def test_adaptive_context_config_rejects_invalid_expansion_fractions() -> None:
    with pytest.raises(ValueError, match="expansion_token_fraction"):
        AdaptiveContextConfig(expansion_token_fraction=0.0)
    with pytest.raises(ValueError, match="expansion_line_fraction"):
        AdaptiveContextConfig(expansion_line_fraction=1.5)
