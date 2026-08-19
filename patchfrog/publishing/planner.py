"""Pure, deterministic publication planner.

    Phase 5 findings + PR diff + publication config -> ReviewPublicationPlan

No network calls, no database session, no LLM call -- everything needed
to decide what gets published is passed in as plain values, and calling
this twice with identical input always produces an identical plan (same
membership, same ordering, same bodies). ``mode`` (dry-run vs publish)
changes nothing about what this planner decides; it only ever gates
whether :mod:`patchfrog.publishing.service` performs the resulting GitHub
write afterwards.

Selection funnel, most to least preferred:

1. Findings below ``config.min_severity`` are omitted outright.
2. Findings that map to a real diff line (see
   :mod:`patchfrog.publishing.diff_mapper`) are ranked (severity desc,
   confidence desc, then stable path/line) and the top
   ``config.max_inline_comments`` become inline comments.
3. Everything else that survived step 1 -- unmappable findings, plus
   inline overflow from step 2 -- is ranked the same way and the top
   ``config.max_summary_findings`` become summary-only entries.
4. Anything left over is recorded as omitted (count only, never silently
   dropped from the summary's own accounting).

Final output ordering (both tuples) is independent of the selection
ranking above -- path, then line, then severity, then fingerprint (a
readability/stability ordering, not a selection one) -- so the same input
always yields the same plan, tuple order included.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from patchfrog.analysis.domain import Confidence, Severity
from patchfrog.diff.models import DiffFile
from patchfrog.domain.pull_request import ChangedFile
from patchfrog.publishing.body import format_inline_comment_body, format_summary_body
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.diff_mapper import map_finding_to_diff_position
from patchfrog.publishing.domain import (
    MappedPosition,
    PublicationDisposition,
    PublishableFinding,
    ReviewInputSnapshot,
    ReviewPublicationComment,
    ReviewPublicationMode,
    ReviewPublicationPlan,
    ReviewPublicationStatus,
)
from patchfrog.publishing.fingerprint import compute_finding_fingerprint

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}
_CONFIDENCE_RANK: dict[Confidence, int] = {
    Confidence.HIGH: 2,
    Confidence.MEDIUM: 1,
    Confidence.LOW: 0,
}


def _meets_min_severity(severity: Severity, *, minimum: Severity) -> bool:
    return _SEVERITY_RANK[severity] >= _SEVERITY_RANK[minimum]


class PublicationPlanner:
    """Stateless; safe to reuse across calls."""

    def build_plan(
        self,
        *,
        publication_id: UUID,
        snapshot: ReviewInputSnapshot,
        findings: Sequence[PublishableFinding],
        changed_files: Sequence[ChangedFile],
        diff_files: Sequence[DiffFile],
        config: PublicationConfig,
        mode: ReviewPublicationMode,
        current_head_sha: str,
    ) -> ReviewPublicationPlan:
        if current_head_sha != snapshot.head_sha:
            return ReviewPublicationPlan(
                snapshot=snapshot,
                mode=mode,
                status=ReviewPublicationStatus.STALE,
                reason=(
                    f"HEAD_SHA_MISMATCH: review was generated for {snapshot.head_sha!r}, "
                    f"but the pull request's current head is {current_head_sha!r}"
                ),
            )

        if not findings:
            return ReviewPublicationPlan(
                snapshot=snapshot,
                mode=mode,
                status=ReviewPublicationStatus.SKIPPED_NO_FINDINGS,
                reason="the review run produced zero findings",
            )

        changed_by_path: Mapping[str, ChangedFile] = {f.path: f for f in changed_files}
        diff_by_path: Mapping[str, DiffFile] = {f.path: f for f in diff_files}

        eligible: list[PublishableFinding] = []
        omitted: list[ReviewPublicationComment] = []
        for finding in findings:
            if not _meets_min_severity(finding.severity, minimum=config.min_severity):
                omitted.append(self._to_comment(finding, snapshot, PublicationDisposition.OMITTED, position=None, reason="below minimum severity threshold"))
                continue
            eligible.append(finding)

        mappable: list[tuple[PublishableFinding, ReviewPublicationComment]] = []
        unmappable: list[tuple[PublishableFinding, ReviewPublicationComment]] = []
        for finding in eligible:
            outcome = map_finding_to_diff_position(
                finding_path=finding.file_path,
                start_line=finding.start_line,
                end_line=finding.end_line,
                changed_file=changed_by_path.get(finding.file_path),
                diff_file=diff_by_path.get(finding.file_path),
            )
            if outcome.is_mappable:
                comment = self._to_comment(
                    finding, snapshot, PublicationDisposition.INLINE, position=outcome.position, reason="mapped to a valid diff line"
                )
                mappable.append((finding, comment))
            else:
                reason = f"unmappable: {outcome.unmappable_reason.value if outcome.unmappable_reason else 'unknown'} ({outcome.detail})"
                comment = self._to_comment(finding, snapshot, PublicationDisposition.SUMMARY_ONLY, position=None, reason=reason)
                unmappable.append((finding, comment))

        mappable.sort(key=lambda pair: self._selection_key(pair[0]))
        inline_selected = mappable[: config.max_inline_comments]
        inline_overflow = mappable[config.max_inline_comments :]

        summary_candidates = [
            (f, self._to_comment(f, snapshot, PublicationDisposition.SUMMARY_ONLY, position=None, reason="exceeded max_inline_comments; preserved in summary"))
            for f, _ in inline_overflow
        ] + unmappable
        summary_candidates.sort(key=lambda pair: self._selection_key(pair[0]))
        summary_selected = summary_candidates[: config.max_summary_findings]
        summary_overflow = summary_candidates[config.max_summary_findings :]

        for finding, _ in summary_overflow:
            omitted.append(
                self._to_comment(finding, snapshot, PublicationDisposition.OMITTED, position=None, reason="exceeded max_summary_findings cap")
            )

        inline_comments = tuple(sorted((c for _, c in inline_selected), key=lambda c: self._presentation_key(c)))
        summary_only = tuple(sorted((c for _, c in summary_selected), key=lambda c: self._presentation_key(c)))
        omitted_tuple = tuple(sorted(omitted, key=lambda c: self._presentation_key(c)))

        if not inline_comments and not summary_only:
            return ReviewPublicationPlan(
                snapshot=snapshot,
                mode=mode,
                status=ReviewPublicationStatus.SKIPPED_NO_FINDINGS,
                reason="no findings met the publication threshold (all filtered or omitted)",
                omitted=omitted_tuple,
            )

        status = ReviewPublicationStatus.DRY_RUN if mode is ReviewPublicationMode.DRY_RUN else ReviewPublicationStatus.PLANNED

        finding_by_id = {f.finding_id: f for f in findings}
        counts_by_severity: dict[Severity, int] = {}
        for finding in findings:
            counts_by_severity[finding.severity] = counts_by_severity.get(finding.severity, 0) + 1
        summary_body, _truncated = format_summary_body(
            publication_id=publication_id,
            counts_by_severity=counts_by_severity,
            inline_findings=[finding_by_id[c.finding_id] for c in inline_comments],
            summary_only_findings=[finding_by_id[c.finding_id] for c in summary_only],
            omitted_count=len(omitted_tuple),
        )

        return ReviewPublicationPlan(
            snapshot=snapshot,
            mode=mode,
            status=status,
            reason=None,
            inline_comments=inline_comments,
            summary_only=summary_only,
            omitted=omitted_tuple,
            summary_body=summary_body,
        )

    @staticmethod
    def _selection_key(finding: PublishableFinding) -> tuple[int, int, str, int, str]:
        return (
            -_SEVERITY_RANK[finding.severity],
            -_CONFIDENCE_RANK[finding.confidence],
            finding.file_path,
            finding.start_line,
            str(finding.finding_id),
        )

    @staticmethod
    def _presentation_key(comment: ReviewPublicationComment) -> tuple[str, int, int, str]:
        line = comment.position.line if comment.position is not None else 0
        return (comment.path, line, -_SEVERITY_RANK[comment.severity], comment.fingerprint)

    @staticmethod
    def _to_comment(
        finding: PublishableFinding,
        snapshot: ReviewInputSnapshot,
        disposition: PublicationDisposition,
        *,
        position: MappedPosition | None,
        reason: str,
    ) -> ReviewPublicationComment:
        fingerprint = compute_finding_fingerprint(
            repository_id=snapshot.repository_id,
            pull_request_number=snapshot.pull_request_number,
            head_sha=snapshot.head_sha,
            finding=finding,
        )
        body = ""
        if disposition == PublicationDisposition.INLINE:
            body, _truncated = format_inline_comment_body(finding)
        return ReviewPublicationComment(
            finding_id=finding.finding_id,
            fingerprint=fingerprint,
            path=finding.file_path,
            body=body,
            severity=finding.severity,
            disposition=disposition,
            reason=reason,
            position=position,
        )
