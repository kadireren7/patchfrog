"""Deterministic derivation of
:class:`~patchfrog.repository_learnings.domain.RepositoryLearning`\\ s
and their current-PR
:class:`~patchfrog.repository_learnings.domain.PotentialRepositoryLearningApplication`\\ s
-- pure, synchronous, consuming only Milestone N's own already-fetched
:class:`~patchfrog.historical_regression_memory.domain.HistoricalRegressionRecord`\\ s
and already-computed J/N current-run objects.

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

See ``validation/repository_learnings/latest-summary.md`` sections
4-10 for the full design narrative behind pattern identity, the
minimum-support gate, activation time, and dedup ownership implemented
here.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID

from patchfrog.change_intelligence.domain import ChangeKind, ChangeUnit
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
    RepositoryLearningApplicationStatus,
    RepositoryLearningEvidence,
    RepositoryLearningPattern,
    RepositoryLearningPatternKind,
    RepositoryLearningStatus,
)


def _parse(observed_at: str) -> datetime:
    return datetime.fromisoformat(observed_at)


def _learning_id(pattern: RepositoryLearningPattern) -> str:
    raw = (
        f"{pattern.repository_id}|{pattern.pattern_kind.value}|"
        f"{pattern.anchor_file_path}|{pattern.anchor_qualified_name}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def derive_repository_learnings(
    *,
    trusted_records: tuple[HistoricalRegressionRecord, ...],
    repository_id: UUID,
) -> tuple[RepositoryLearning, ...]:
    """Group N's already-trusted, already-point-in-time-filtered
    records by exact surface identity, keep only one representative
    record per *distinct historical review run* (the independence
    rule -- two findings from the same run never count twice), and
    only construct a :class:`RepositoryLearning` when the
    review-run-distinct count reaches
    :data:`~patchfrog.repository_learnings.domain.MIN_SUPPORTING_EVENTS`.
    A record with no ``source_qualified_name`` never participates --
    see the domain module's own docstring on why file-only identity is
    unsafe here."""

    groups: dict[tuple[str, str], dict[UUID, HistoricalRegressionRecord]] = {}
    for record in trusted_records:
        if record.source_qualified_name is None:
            continue
        key = (record.source_file_path, record.source_qualified_name)
        by_run = groups.setdefault(key, {})
        existing = by_run.get(record.historical_review_run_id)
        if existing is None or _parse(record.observed_at) < _parse(existing.observed_at):
            by_run[record.historical_review_run_id] = record

    learnings: list[RepositoryLearning] = []
    for (file_path, qualified_name), by_run in groups.items():
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
            finding_category=ordered[0].finding_category,
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
    change_units: tuple[ChangeUnit, ...],
    historical_candidates: tuple[PotentialHistoricalRegression, ...] = (),
) -> tuple[PotentialRepositoryLearningApplication, ...]:
    """A learning applies to the current PR only when its exact anchor
    ``(file_path, qualified_name)`` is *directly changed* by a non-TEST
    ``ChangeUnit`` -- mirrors N's own ``ChangeKind.TEST`` exclusion
    exactly, so a test-only PR that merely calls a learned-risky symbol
    never triggers an application (the same failure mode both M's and
    N's own corpora already caught once, see the audit's section 9).

    Every real application is constructed with
    ``status=UNSATISFIED`` (see the domain module's own docstring for
    why -- ``REPEATED_SAME_SURFACE_REGRESSION`` has no companion target
    to satisfy). Enriches an existing N candidate on the same exact
    surface when one is present -- never hard-coded to assume one
    always exists (see the audit's "Dedup ownership" section)."""

    non_test_units = tuple(u for u in change_units if u.change_kind is not ChangeKind.TEST)
    directly_changed: dict[tuple[str, str], str] = {}
    for unit in non_test_units:
        for candidate in unit.changed_candidates:
            if candidate.qualified_name is None:
                continue
            key = (candidate.file_path, candidate.qualified_name)
            if key not in directly_changed:
                directly_changed[key] = unit.id

    out: list[PotentialRepositoryLearningApplication] = []
    for learning in learnings:
        key = (learning.pattern.anchor_file_path, learning.pattern.anchor_qualified_name)
        change_unit_id = directly_changed.get(key)
        if change_unit_id is None:
            continue

        enriches: PotentialHistoricalRegression | None = None
        for hc in historical_candidates:
            if (
                hc.current_file_path == learning.pattern.anchor_file_path
                and hc.current_qualified_name == learning.pattern.anchor_qualified_name
            ):
                enriches = hc
                break

        evidence = (
            f"this exact surface has produced {learning.support_count} independently trusted "
            f"findings across separate reviews (first {learning.first_observed_at}, "
            f"most recent {learning.last_observed_at})"
        )

        out.append(
            PotentialRepositoryLearningApplication(
                learning=learning,
                current_change_unit_id=change_unit_id,
                current_file_path=learning.pattern.anchor_file_path,
                current_qualified_name=learning.pattern.anchor_qualified_name,
                status=RepositoryLearningApplicationStatus.UNSATISFIED,
                evidence=evidence,
                enriches_historical_regression=enriches,
            )
        )
        if len(out) >= MAX_APPLICATIONS_PER_RUN:
            break

    return tuple(out)
