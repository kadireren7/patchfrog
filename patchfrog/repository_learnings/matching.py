"""Deterministic derivation of
:class:`~patchfrog.repository_learnings.domain.RepositoryLearning`\\ s
and their current-PR
:class:`~patchfrog.repository_learnings.domain.PotentialRepositoryLearningApplication`\\ s
-- pure, synchronous, consuming only Milestone N's own already-fetched
:class:`~patchfrog.historical_regression_memory.domain.HistoricalRegressionRecord`\\ s
and already-computed N candidates.

**No database session, no query of its own at all** -- unlike N, this
package issues zero SQL. Every trusted record it needs was already
fetched, once, by N's own bounded query
(``fetch_trusted_historical_records``) earlier in the same review run;
this package only groups and counts those already-correct,
already-point-in-time-filtered records. See
``validation/repository_learnings/latest-summary.md`` section 2 for
why this is safe: reusing N's exact eligibility/temporal logic means a
future correction to trust semantics fixes both packages at once, and
there is never a second, potentially-divergent trust model to keep in
sync.

**Applications never re-derive current-PR relevance.** An external
-review correction round found the original v1 shape independently
checked whether the learning's anchor was directly changed (via its
own ``ChangeUnit``/``ChangeKind.TEST`` walk), which meant O could
construct a standalone application even when N found nothing --
making O a second, independent historical-regression detector. Fixed:
:func:`derive_repository_learning_applications` now takes no
``change_units`` at all -- it only enriches an existing N candidate on
the exact same surface, using *that* candidate's own already-correct
current-PR identity (``current_change_unit_id``/``current_file_path``/
``current_qualified_name``). When no N candidate exists on a learning's
surface this run, that learning produces no application at all.

See ``validation/repository_learnings/latest-summary.md`` sections
4-10 for the full design narrative behind pattern identity, the
minimum-support gate, and activation time implemented here.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

from patchfrog.analysis.domain import FindingCategory
from patchfrog.historical_regression_memory.domain import (
    HistoricalRegressionRecord,
    PotentialHistoricalRegression,
)
from patchfrog.repository_learnings.domain import (
    MAX_APPLICATIONS_PER_RUN,
    MAX_LEARNINGS_PER_RUN,
    MAX_SUPPORTING_EVENTS_PER_LEARNING,
    MIN_SUPPORTING_EVENTS,
    PotentialRepositoryLearningApplication,
    RepositoryLearning,
    RepositoryLearningEvidence,
    RepositoryLearningPattern,
    RepositoryLearningPatternKind,
    RepositoryLearningStatus,
)

_SurfaceKey = tuple[str, str, FindingCategory]


def _parse(observed_at: str) -> datetime:
    return datetime.fromisoformat(observed_at)


def _learning_id(pattern: RepositoryLearningPattern) -> str:
    raw = (
        f"{pattern.repository_id}|{pattern.pattern_kind.value}|{pattern.anchor_file_path}|"
        f"{pattern.anchor_qualified_name}|{pattern.finding_category.value}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def derive_repository_learnings(
    *,
    trusted_records: tuple[HistoricalRegressionRecord, ...],
    repository_id: UUID,
) -> tuple[RepositoryLearning, ...]:
    """Group N's already-trusted, already-point-in-time-filtered
    records by exact surface identity -- ``(file_path, qualified_name,
    finding_category)``, **category included** (see
    :class:`~patchfrog.repository_learnings.domain.RepositoryLearningPattern`'s
    own docstring for why: two trusted findings on the same symbol but
    a genuinely different category are not necessarily one repeated
    technical pattern, and there is no richer root-cause identity
    persisted anywhere to tell them apart). Keeps only one
    representative record per *distinct historical review run* (the
    independence rule -- two findings from the same run never count
    twice), and only constructs a :class:`RepositoryLearning` when the
    review-run-distinct count reaches
    :data:`~patchfrog.repository_learnings.domain.MIN_SUPPORTING_EVENTS`.
    A record with no ``source_qualified_name`` never participates --
    see the domain module's own docstring on why file-only identity is
    unsafe here."""

    groups: dict[_SurfaceKey, dict[UUID, HistoricalRegressionRecord]] = {}
    for record in trusted_records:
        if record.source_qualified_name is None:
            continue
        key = (record.source_file_path, record.source_qualified_name, record.finding_category)
        by_run = groups.setdefault(key, {})
        existing = by_run.get(record.historical_review_run_id)
        if existing is None or _parse(record.observed_at) < _parse(existing.observed_at):
            by_run[record.historical_review_run_id] = record

    learnings: list[RepositoryLearning] = []
    for (file_path, qualified_name, category), by_run in groups.items():
        if len(by_run) < MIN_SUPPORTING_EVENTS:
            continue

        ordered = sorted(by_run.values(), key=lambda r: (_parse(r.observed_at), r.historical_finding_id))
        bounded = ordered[:MAX_SUPPORTING_EVENTS_PER_LEARNING]
        # Activation is defined over the *minimum* support set -- the
        # earliest MIN_SUPPORTING_EVENTS runs -- regardless of how many
        # additional runs exist beyond it.
        activated_at = ordered[MIN_SUPPORTING_EVENTS - 1].observed_at

        pattern = RepositoryLearningPattern(
            repository_id=repository_id,
            pattern_kind=RepositoryLearningPatternKind.REPEATED_SAME_SURFACE_REGRESSION,
            anchor_file_path=file_path,
            anchor_qualified_name=qualified_name,
            finding_category=category,
        )

        learnings.append(
            RepositoryLearning(
                learning_id=_learning_id(pattern),
                pattern=pattern,
                status=RepositoryLearningStatus.ACTIVE,
                supporting_evidence=tuple(RepositoryLearningEvidence(historical_record=r) for r in bounded),
                support_count=len(by_run),
                activated_at=activated_at,
                first_observed_at=ordered[0].observed_at,
                last_observed_at=ordered[-1].observed_at,
            )
        )
        if len(learnings) >= MAX_LEARNINGS_PER_RUN:
            break

    return tuple(learnings)


def derive_repository_learning_applications(
    *,
    learnings: tuple[RepositoryLearning, ...],
    historical_candidates: tuple[PotentialHistoricalRegression, ...] = (),
) -> tuple[PotentialRepositoryLearningApplication, ...]:
    """A learning applies to the current PR **only when an existing
    Milestone N candidate already exists on the exact same surface**
    this run -- never re-derived independently from ``ChangeUnit``s
    (see this module's own docstring). N's own candidate already
    encodes every current-relevance rule this milestone lineage
    established (direct change vs. affected-surface, the
    ``ChangeKind.TEST`` exclusion, dedup ownership) -- reusing it here
    means O can never rediscover current relevance N itself did not
    find, and a learning whose surface N does not currently flag
    simply produces no application at all.

    Every real application enriches that N candidate
    (``enriches_historical_regression``, mandatory) and carries no
    ``status`` -- see
    :class:`~patchfrog.repository_learnings.domain.PotentialRepositoryLearningApplication`'s
    own docstring for why."""

    n_by_surface: dict[tuple[str, str | None], PotentialHistoricalRegression] = {}
    for hc in historical_candidates:
        key = (hc.current_file_path, hc.current_qualified_name)
        n_by_surface.setdefault(key, hc)

    out: list[PotentialRepositoryLearningApplication] = []
    for learning in learnings:
        key = (learning.pattern.anchor_file_path, learning.pattern.anchor_qualified_name)
        enriches = n_by_surface.get(key)
        if enriches is None:
            continue

        evidence = (
            f"this exact surface has produced {learning.support_count} independently trusted "
            f"{learning.pattern.finding_category.value} findings across separate, independent "
            f"reviews (first {learning.first_observed_at}, most recent {learning.last_observed_at})"
        )

        out.append(
            PotentialRepositoryLearningApplication(
                learning=learning,
                current_change_unit_id=enriches.current_change_unit_id,
                current_file_path=enriches.current_file_path,
                current_qualified_name=enriches.current_qualified_name,
                evidence=evidence,
                enriches_historical_regression=enriches,
            )
        )
        if len(out) >= MAX_APPLICATIONS_PER_RUN:
            break

    return tuple(out)
