"""Deterministic context scoring.

Every weight below is an explicit constant, tested individually (see
``tests/unit/test_context_scoring.py``) -- no learned or inferred weight,
no LLM judgment. A score is always accompanied by its
:class:`~patchfrog.context.domain.ScoreComponent` breakdown so a caller
can see exactly why an item ranked where it did.
"""

from __future__ import annotations

from patchfrog.analysis.domain import FindingCategory
from patchfrog.context.domain import ContextCandidate, ContextRelationship, ScoreComponent

#: Base score per relationship kind -- the dominant signal. Ordering here
#: mirrors the "ranking priorities" default: target > tests > direct
#: caller/callee > structural containment > import/include > sibling >
#: transitive (depth-2) relations.
_RELATIONSHIP_BASE_SCORE: dict[ContextRelationship, float] = {
    ContextRelationship.TARGET_SYMBOL: 1.00,
    ContextRelationship.TESTS_TARGET_FILE: 0.55,
    ContextRelationship.DIRECT_CALLER: 0.50,
    ContextRelationship.DIRECT_CALLEE: 0.50,
    ContextRelationship.PARENT_SYMBOL: 0.45,
    ContextRelationship.IMPORT_DEPENDENCY: 0.30,
    ContextRelationship.INCLUDE_DEPENDENCY: 0.30,
    ContextRelationship.SIBLING_SYMBOL: 0.20,
    ContextRelationship.TRANSITIVE_CALLER: 0.25,
    ContextRelationship.TRANSITIVE_CALLEE: 0.25,
}

#: Explicit tie-break order (lower index wins), used whenever two items
#: have an identical score -- never left to incidental DB row order.
RELATIONSHIP_PRIORITY: tuple[ContextRelationship, ...] = (
    ContextRelationship.TARGET_SYMBOL,
    ContextRelationship.TESTS_TARGET_FILE,
    ContextRelationship.DIRECT_CALLER,
    ContextRelationship.DIRECT_CALLEE,
    ContextRelationship.PARENT_SYMBOL,
    ContextRelationship.IMPORT_DEPENDENCY,
    ContextRelationship.INCLUDE_DEPENDENCY,
    ContextRelationship.SIBLING_SYMBOL,
    ContextRelationship.TRANSITIVE_CALLER,
    ContextRelationship.TRANSITIVE_CALLEE,
)

_SAME_FILE_BONUS = 0.10
_CHANGED_LINE_BONUS = 0.15
_DISTANCE_PENALTY_PER_HOP = 0.05
_FINDING_CATEGORY_BONUS = 0.08

#: Deliberately small and explicit -- only the categories called out as
#: examples get a preference table at all; every other category (or no
#: finding) scores purely on relationship/distance/changed-code signals.
_FINDING_CATEGORY_PREFERRED_RELATIONSHIPS: dict[FindingCategory, frozenset[ContextRelationship]] = {
    FindingCategory.MEMORY_SAFETY: frozenset(
        {ContextRelationship.DIRECT_CALLER, ContextRelationship.DIRECT_CALLEE}
    ),
    FindingCategory.RESOURCE_MANAGEMENT: frozenset(
        {ContextRelationship.DIRECT_CALLER, ContextRelationship.DIRECT_CALLEE}
    ),
    FindingCategory.CONCURRENCY: frozenset(
        {ContextRelationship.DIRECT_CALLER, ContextRelationship.DIRECT_CALLEE}
    ),
    FindingCategory.API_MISUSE: frozenset(
        {
            ContextRelationship.DIRECT_CALLEE,
            ContextRelationship.IMPORT_DEPENDENCY,
            ContextRelationship.INCLUDE_DEPENDENCY,
        }
    ),
    FindingCategory.CORRECTNESS: frozenset({ContextRelationship.TESTS_TARGET_FILE}),
}


def score_candidate(
    candidate: ContextCandidate,
    *,
    target_file_path: str,
    finding_category: FindingCategory | None,
) -> tuple[float, tuple[ScoreComponent, ...]]:
    components: list[ScoreComponent] = []

    base = _RELATIONSHIP_BASE_SCORE[candidate.relationship]
    components.append(ScoreComponent(label=candidate.relationship.value, value=base))

    if candidate.file_path == target_file_path and candidate.relationship is not ContextRelationship.TARGET_SYMBOL:
        components.append(ScoreComponent(label="same_file", value=_SAME_FILE_BONUS))

    if candidate.is_on_changed_line:
        components.append(ScoreComponent(label="changed_line", value=_CHANGED_LINE_BONUS))

    if candidate.distance > 1:
        penalty = -_DISTANCE_PENALTY_PER_HOP * (candidate.distance - 1)
        components.append(ScoreComponent(label="distance_penalty", value=penalty))

    if finding_category is not None:
        preferred = _FINDING_CATEGORY_PREFERRED_RELATIONSHIPS.get(finding_category, frozenset())
        if candidate.relationship in preferred:
            components.append(
                ScoreComponent(label=f"finding_category:{finding_category.value}", value=_FINDING_CATEGORY_BONUS)
            )

    score = max(0.0, sum(c.value for c in components))
    return score, tuple(components)


def relationship_priority_index(relationship: ContextRelationship) -> int:
    """Lower is higher-priority -- used as an explicit tie-break key."""

    try:
        return RELATIONSHIP_PRIORITY.index(relationship)
    except ValueError:
        return len(RELATIONSHIP_PRIORITY)
