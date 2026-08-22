"""Privacy-conscious feedback export (Phase 9 spec section 27).

Exports one JSON object per line (JSONL), keyed on finding identity and
feedback signals only. Deliberately excludes:

- usernames/actor logins (feedback counts are exported, not who gave
  them),
- raw private repository source (only a hash of the finding's evidence
  is included, never the evidence text itself),
- reply bodies, unless the caller explicitly opts in with
  ``include_reply_bodies=True`` (never the default).

This is meant to support future manual tuning decisions, not automatic
training -- see docs/feedback.md.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from patchfrog.feedback.domain import FindingFeedbackSummary
from patchfrog.feedback.queries import get_feedback_summary
from patchfrog.persistence.models.review import AIFindingModel


def _evidence_hash(evidence_json: str) -> str:
    return hashlib.sha256(evidence_json.encode()).hexdigest()


async def _finding_metadata(
    session: AsyncSession, *, finding_ids: list[uuid.UUID]
) -> dict[uuid.UUID, AIFindingModel]:
    if not finding_ids:
        return {}
    result = await session.execute(select(AIFindingModel).where(AIFindingModel.id.in_(finding_ids)))
    return {m.id: m for m in result.scalars().all()}


def _record_for_summary(
    summary: FindingFeedbackSummary, *, finding: AIFindingModel | None, reply_bodies: list[str] | None
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "finding_id": str(summary.finding_id),
        "category": finding.category.value if finding is not None else None,
        "severity": finding.severity.value if finding is not None else None,
        "confidence_band": finding.confidence.value if finding is not None else None,
        "evidence_hash": _evidence_hash(finding.evidence) if finding is not None else None,
        "positive_reactions": summary.positive_reactions,
        "negative_reactions": summary.negative_reactions,
        "developer_replies": summary.developer_replies,
        "explicit_useful": summary.explicit_useful,
        "explicit_false_positive": summary.explicit_false_positive,
        "explicit_fixed": summary.explicit_fixed,
        "explicit_ignore": summary.explicit_ignore,
        "thread_resolved": summary.thread_resolved,
        "finding_changed": summary.finding_changed,
        "finding_disappeared": summary.finding_disappeared,
        "usefulness_signal": summary.assessment.usefulness_signal.value,
        "correctness_signal": summary.assessment.correctness_signal.value,
        "resolution_signal": summary.assessment.resolution_signal.value,
        "engagement_signal": summary.assessment.engagement_signal,
        "confidence": summary.assessment.confidence.value if summary.assessment.confidence else None,
        "reasons": list(summary.assessment.reasons),
        "assessment_version": summary.assessment.assessment_version,
    }
    if reply_bodies is not None:
        record["reply_bodies"] = reply_bodies
    return record


async def build_export_records(
    session: AsyncSession,
    *,
    repository_id: uuid.UUID | None = None,
    include_reply_bodies: bool = False,
) -> list[dict[str, Any]]:
    summaries = await get_feedback_summary(session, repository_id=repository_id)
    findings = await _finding_metadata(session, finding_ids=[s.finding_id for s in summaries])

    records: list[dict[str, Any]] = []
    for summary in summaries:
        reply_bodies: list[str] | None = None
        if include_reply_bodies:
            # Reply bodies are never persisted on FeedbackEvent (see
            # patchfrog.feedback.domain's module docstring) -- there is
            # nothing to include even when explicitly requested. This
            # branch exists so a future caller that does start
            # persisting reply text has exactly one place to wire it in,
            # without silently changing export shape today.
            reply_bodies = []
        records.append(
            _record_for_summary(summary, finding=findings.get(summary.finding_id), reply_bodies=reply_bodies)
        )
    return records


def write_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
