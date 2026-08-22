"""Operational feedback-quality metrics (Phase 9 spec sections 20/34/45).

Deliberately its own, separate report shape from Phase 8's benchmark
metrics (:mod:`patchfrog.evaluation.metrics`) -- these are never merged
into a benchmark's precision/recall/F1. Phase 8 measures PatchFrog
against human-authored ground truth; this module measures how real
developers reacted to what actually got published. Mixing the two would
make a precision number mean two different things depending on which
report you're reading, which is exactly the confusion Phase 9 spec
section 24 warns against.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.feedback.domain import FindingFeedbackSummary, SignalPolarity
from patchfrog.feedback.queries import get_feedback_summary
from patchfrog.persistence.models.review import AIFindingModel
from patchfrog.persistence.repositories.feedback import FeedbackEventRepository


@dataclass(frozen=True, slots=True)
class SliceMetrics:
    key: str
    findings_with_feedback: int
    positive_hint_rate: float
    negative_hint_rate: float
    explicit_false_positive_count: int


@dataclass(frozen=True, slots=True)
class ProductionFeedbackMetrics:
    feedback_events_ingested: int
    unique_findings_with_feedback: int
    positive_hint_rate: float
    negative_hint_rate: float
    explicit_useful_count: int
    explicit_false_positive_count: int
    explicit_fixed_count: int
    developer_engagement_rate: float
    unattributed_event_count: int
    assessment_confidence_distribution: dict[str, int]
    by_category: tuple[SliceMetrics, ...] = field(default_factory=tuple)
    by_severity: tuple[SliceMetrics, ...] = field(default_factory=tuple)
    by_confidence_band: tuple[SliceMetrics, ...] = field(default_factory=tuple)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _slice(key: str, summaries: list[FindingFeedbackSummary]) -> SliceMetrics:
    total = len(summaries)
    positive = sum(1 for s in summaries if s.assessment.usefulness_signal is SignalPolarity.POSITIVE)
    negative = sum(1 for s in summaries if s.assessment.usefulness_signal is SignalPolarity.NEGATIVE)
    explicit_fp = sum(1 for s in summaries if s.explicit_false_positive > 0)
    return SliceMetrics(
        key=key,
        findings_with_feedback=total,
        positive_hint_rate=_rate(positive, total),
        negative_hint_rate=_rate(negative, total),
        explicit_false_positive_count=explicit_fp,
    )


async def compute_production_feedback_metrics(
    session: AsyncSession, *, repository_id: uuid.UUID | None = None
) -> ProductionFeedbackMetrics:
    summaries = await get_feedback_summary(session, repository_id=repository_id)

    finding_ids = [s.finding_id for s in summaries]
    findings: dict[uuid.UUID, AIFindingModel] = {}
    if finding_ids:
        result = await session.execute(select(AIFindingModel).where(AIFindingModel.id.in_(finding_ids)))
        findings = {m.id: m for m in result.scalars().all()}

    total = len(summaries)
    positive = sum(1 for s in summaries if s.assessment.usefulness_signal is SignalPolarity.POSITIVE)
    negative = sum(1 for s in summaries if s.assessment.usefulness_signal is SignalPolarity.NEGATIVE)
    engaged = sum(1 for s in summaries if s.assessment.engagement_signal)
    events_ingested = sum(
        s.positive_reactions
        + s.negative_reactions
        + s.developer_replies
        + s.explicit_useful
        + s.explicit_false_positive
        + s.explicit_fixed
        + s.explicit_ignore
        for s in summaries
    )
    confidence_distribution = Counter(
        s.assessment.confidence.value if s.assessment.confidence is not None else "none" for s in summaries
    )
    unattributed_count = len(
        await FeedbackEventRepository().list_unattributed(session, repository_id=repository_id)
    )

    by_category: dict[str, list[FindingFeedbackSummary]] = defaultdict(list)
    by_severity: dict[str, list[FindingFeedbackSummary]] = defaultdict(list)
    by_confidence_band: dict[str, list[FindingFeedbackSummary]] = defaultdict(list)
    for summary in summaries:
        finding = findings.get(summary.finding_id)
        if finding is None:
            continue
        by_category[finding.category.value].append(summary)
        by_severity[finding.severity.value].append(summary)
        by_confidence_band[finding.confidence.value].append(summary)

    return ProductionFeedbackMetrics(
        feedback_events_ingested=events_ingested,
        unique_findings_with_feedback=total,
        positive_hint_rate=_rate(positive, total),
        negative_hint_rate=_rate(negative, total),
        explicit_useful_count=sum(s.explicit_useful for s in summaries),
        explicit_false_positive_count=sum(s.explicit_false_positive for s in summaries),
        explicit_fixed_count=sum(s.explicit_fixed for s in summaries),
        developer_engagement_rate=_rate(engaged, total),
        unattributed_event_count=unattributed_count,
        assessment_confidence_distribution=dict(confidence_distribution),
        by_category=tuple(_slice(k, v) for k, v in sorted(by_category.items())),
        by_severity=tuple(_slice(k, v) for k, v in sorted(by_severity.items())),
        by_confidence_band=tuple(_slice(k, v) for k, v in sorted(by_confidence_band.items())),
    )
