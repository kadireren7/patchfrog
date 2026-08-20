"""Unit coverage for Phase 7's ``already_reported_finding_ids`` hook into
:meth:`patchfrog.publishing.planner.PublicationPlanner.build_plan` --
never weakens Phase 6's own exact-SHA publication idempotency, only
narrows which findings enter planning at all (see the module docstring
of :mod:`patchfrog.review_memory.fingerprint`)."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import Confidence, FindingCategory, Severity
from patchfrog.diff.models import DiffFile
from patchfrog.diff.parser import build_diff_file
from patchfrog.domain.pull_request import ChangedFile, FileChangeStatus
from patchfrog.publishing.config import PublicationConfig
from patchfrog.publishing.domain import (
    PublicationDisposition,
    PublishableFinding,
    ReviewInputSnapshot,
    ReviewPublicationMode,
    ReviewPublicationStatus,
)
from patchfrog.publishing.planner import PublicationPlanner

_HEAD_SHA = "a" * 40
_REPOSITORY_ID = uuid.uuid4()
_REVIEW_RUN_ID = uuid.uuid4()


def _snapshot() -> ReviewInputSnapshot:
    return ReviewInputSnapshot(
        repository_id=_REPOSITORY_ID, repository_full_name="test/repo", pull_request_number=1,
        review_run_id=_REVIEW_RUN_ID, head_sha=_HEAD_SHA,
    )


def _finding(*, line: int, title: str) -> PublishableFinding:
    return PublishableFinding(
        finding_id=uuid.uuid4(), title=title, message=f"{title} message", category=FindingCategory.CORRECTNESS,
        severity=Severity.HIGH, confidence=Confidence.HIGH, file_path="src/billing.py",
        start_line=line, end_line=line, reasoning_summary="because", suggested_fix=None,
    )


def _whole_file_changed_files(n_lines: int = 100) -> tuple[list[ChangedFile], list[DiffFile]]:
    patch = "@@ -0,0 +1,{} @@\n{}".format(n_lines, "\n".join(f"+line{i}" for i in range(1, n_lines + 1)))
    changed_file = ChangedFile(
        path="src/billing.py", previous_path=None, status=FileChangeStatus.ADDED,
        additions=n_lines, deletions=0, patch=patch,
    )
    diff_file = build_diff_file(changed_file.path, changed_file.patch)
    return [changed_file], [diff_file]


def test_already_reported_finding_is_suppressed_not_republished() -> None:
    carried = _finding(line=10, title="carried bug")
    fresh = _finding(line=20, title="new bug")
    changed_files, diff_files = _whole_file_changed_files()

    plan = PublicationPlanner().build_plan(
        publication_id=uuid.uuid4(), snapshot=_snapshot(), findings=[carried, fresh],
        changed_files=changed_files, diff_files=diff_files, config=PublicationConfig(min_severity=Severity.INFO),
        mode=ReviewPublicationMode.DRY_RUN, current_head_sha=_HEAD_SHA,
        already_reported_finding_ids=frozenset({carried.finding_id}),
    )

    inline_ids = {c.finding_id for c in plan.inline_comments}
    assert carried.finding_id not in inline_ids
    assert fresh.finding_id in inline_ids
    already_reported_ids = {c.finding_id for c in plan.already_reported}
    assert already_reported_ids == {carried.finding_id}
    assert plan.already_reported[0].disposition is PublicationDisposition.ALREADY_REPORTED


def test_already_reported_never_counted_as_omitted() -> None:
    carried = _finding(line=10, title="carried bug")
    changed_files, diff_files = _whole_file_changed_files()

    plan = PublicationPlanner().build_plan(
        publication_id=uuid.uuid4(), snapshot=_snapshot(), findings=[carried],
        changed_files=changed_files, diff_files=diff_files, config=PublicationConfig(min_severity=Severity.INFO),
        mode=ReviewPublicationMode.DRY_RUN, current_head_sha=_HEAD_SHA,
        already_reported_finding_ids=frozenset({carried.finding_id}),
    )
    assert plan.omitted == ()
    assert len(plan.already_reported) == 1
    assert plan.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS


def test_no_suppression_set_behaves_exactly_like_before() -> None:
    finding = _finding(line=10, title="bug")
    changed_files, diff_files = _whole_file_changed_files()

    plan = PublicationPlanner().build_plan(
        publication_id=uuid.uuid4(), snapshot=_snapshot(), findings=[finding],
        changed_files=changed_files, diff_files=diff_files, config=PublicationConfig(min_severity=Severity.INFO),
        mode=ReviewPublicationMode.DRY_RUN, current_head_sha=_HEAD_SHA,
    )
    assert plan.already_reported == ()
    assert {c.finding_id for c in plan.inline_comments} == {finding.finding_id}


def test_suppression_is_keyed_by_this_runs_finding_id_not_semantic_identity() -> None:
    """already_reported_finding_ids is populated by the caller (Phase 7)
    with THIS run's own AIFindingModel ids that were recheck-confirmed
    carries -- the planner itself does no semantic matching, it is a
    plain id-set filter. A finding_id absent from the set is never
    suppressed, however similar its content."""

    finding = _finding(line=10, title="bug")
    changed_files, diff_files = _whole_file_changed_files()

    plan = PublicationPlanner().build_plan(
        publication_id=uuid.uuid4(), snapshot=_snapshot(), findings=[finding],
        changed_files=changed_files, diff_files=diff_files, config=PublicationConfig(min_severity=Severity.INFO),
        mode=ReviewPublicationMode.DRY_RUN, current_head_sha=_HEAD_SHA,
        already_reported_finding_ids=frozenset({uuid.uuid4()}),  # some other finding entirely
    )
    assert {c.finding_id for c in plan.inline_comments} == {finding.finding_id}
    assert plan.already_reported == ()
