"""Unit coverage for patchfrog.publishing.planner -- deterministic
ordering, severity threshold, max-comment cap, summary fallback,
duplicate findings, unmappable findings, empty findings, exact same
input -> exact same plan. Pure/synchronous, no network, no database."""

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
    ReviewPublicationPlan,
    ReviewPublicationStatus,
)
from patchfrog.publishing.planner import PublicationPlanner

_HEAD_SHA = "a" * 40
_REPOSITORY_ID = uuid.uuid4()
_REVIEW_RUN_ID = uuid.uuid4()


def _snapshot(**overrides: object) -> ReviewInputSnapshot:
    defaults: dict[str, object] = {
        "repository_id": _REPOSITORY_ID,
        "repository_full_name": "test/repo",
        "pull_request_number": 1,
        "review_run_id": _REVIEW_RUN_ID,
        "head_sha": _HEAD_SHA,
    }
    defaults.update(overrides)
    return ReviewInputSnapshot(**defaults)  # type: ignore[arg-type]


def _finding(
    *,
    line: int = 12,
    severity: Severity = Severity.MEDIUM,
    confidence: Confidence = Confidence.MEDIUM,
    path: str = "src/billing.py",
    title: str = "finding",
) -> PublishableFinding:
    return PublishableFinding(
        finding_id=uuid.uuid4(),
        title=title,
        message=f"{title} message",
        category=FindingCategory.CORRECTNESS,
        severity=severity,
        confidence=confidence,
        file_path=path,
        start_line=line,
        end_line=line,
        reasoning_summary="because",
        suggested_fix=None,
    )


def _whole_file_changed_files(
    path: str = "src/billing.py", n_lines: int = 100
) -> tuple[list[ChangedFile], list[DiffFile]]:
    patch = "@@ -0,0 +1,{} @@\n{}".format(n_lines, "\n".join(f"+line{i}" for i in range(1, n_lines + 1)))
    changed_file = ChangedFile(path=path, previous_path=None, status=FileChangeStatus.ADDED, additions=n_lines, deletions=0, patch=patch)
    diff_file = build_diff_file(changed_file.path, changed_file.patch)
    return [changed_file], [diff_file]


def _build(
    findings: list[PublishableFinding],
    config: PublicationConfig | None = None,
    mode: ReviewPublicationMode = ReviewPublicationMode.DRY_RUN,
    current_head_sha: str = _HEAD_SHA,
    publication_id: uuid.UUID | None = None,
) -> ReviewPublicationPlan:
    changed_files, diff_files = _whole_file_changed_files()
    planner = PublicationPlanner()
    return planner.build_plan(
        publication_id=publication_id or uuid.uuid4(),
        snapshot=_snapshot(),
        findings=findings,
        changed_files=changed_files,
        diff_files=diff_files,
        config=config or PublicationConfig(min_severity=Severity.INFO),
        mode=mode,
        current_head_sha=current_head_sha,
    )


def test_empty_findings_is_skipped_no_findings() -> None:
    plan = _build([])
    assert plan.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS
    assert plan.inline_comments == ()
    assert plan.summary_only == ()
    assert plan.summary_body == ""


def test_empty_findings_with_post_clean_summary_enabled_produces_a_publishable_plan() -> None:
    config = PublicationConfig(min_severity=Severity.INFO, post_clean_summary=True)
    plan = _build([], config=config)
    assert plan.status is ReviewPublicationStatus.DRY_RUN
    assert plan.is_publishable
    assert plan.inline_comments == ()
    assert plan.summary_only == ()
    assert "no publishable findings" in plan.summary_body
    assert "No issues exist" not in plan.summary_body


def test_empty_findings_with_post_clean_summary_enabled_publish_mode_is_planned() -> None:
    config = PublicationConfig(min_severity=Severity.INFO, post_clean_summary=True)
    plan = _build([], config=config, mode=ReviewPublicationMode.PUBLISH)
    assert plan.status is ReviewPublicationStatus.PLANNED
    assert plan.is_publishable


def test_all_findings_omitted_never_posts_a_clean_summary_even_when_enabled() -> None:
    """post_clean_summary only ever applies when Phase 5 produced
    genuinely zero findings -- a finding that exists but was filtered
    below the severity threshold is real information PatchFrog knows
    about; claiming "no publishable findings" would be misleading."""

    config = PublicationConfig(min_severity=Severity.HIGH, post_clean_summary=True)
    plan = _build([_finding(severity=Severity.LOW)], config=config)
    assert plan.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS
    assert plan.summary_body == ""


def test_all_findings_already_reported_never_posts_a_clean_summary_even_when_enabled() -> None:
    config = PublicationConfig(min_severity=Severity.INFO, post_clean_summary=True)
    finding = _finding()
    changed_files, diff_files = _whole_file_changed_files()
    plan = PublicationPlanner().build_plan(
        publication_id=uuid.uuid4(),
        snapshot=_snapshot(),
        findings=[finding],
        changed_files=changed_files,
        diff_files=diff_files,
        config=config,
        mode=ReviewPublicationMode.DRY_RUN,
        current_head_sha=_HEAD_SHA,
        already_reported_finding_ids=frozenset({finding.finding_id}),
    )
    assert plan.status is ReviewPublicationStatus.SKIPPED_NO_FINDINGS
    assert plan.summary_body == ""


def test_stale_head_never_produces_comments() -> None:
    plan = _build([_finding()], current_head_sha="b" * 40)
    assert plan.status is ReviewPublicationStatus.STALE
    assert plan.inline_comments == ()
    assert plan.summary_only == ()
    assert plan.reason is not None and "HEAD_SHA_MISMATCH" in plan.reason


def test_mappable_finding_becomes_inline() -> None:
    plan = _build([_finding(line=5)])
    assert len(plan.inline_comments) == 1
    assert plan.inline_comments[0].disposition is PublicationDisposition.INLINE
    assert plan.inline_comments[0].position is not None


def test_unmappable_finding_falls_back_to_summary_not_dropped() -> None:
    finding = _finding(path="src/does_not_exist.py")
    plan = _build([finding])
    assert plan.inline_comments == ()
    assert len(plan.summary_only) == 1
    assert plan.summary_only[0].disposition is PublicationDisposition.SUMMARY_ONLY
    assert "unmappable" in plan.summary_only[0].reason


def test_severity_threshold_omits_below_minimum() -> None:
    low = _finding(line=5, severity=Severity.LOW)
    high = _finding(line=6, severity=Severity.HIGH)
    plan = _build([low, high], config=PublicationConfig(min_severity=Severity.HIGH))

    assert len(plan.inline_comments) == 1
    assert plan.inline_comments[0].severity is Severity.HIGH
    assert len(plan.omitted) == 1
    assert plan.omitted[0].severity is Severity.LOW
    assert "severity" in plan.omitted[0].reason


def test_max_inline_comments_cap_demotes_overflow_to_summary() -> None:
    findings = [_finding(line=i, severity=Severity.HIGH) for i in range(2, 7)]  # 5 findings
    plan = _build(findings, config=PublicationConfig(min_severity=Severity.INFO, max_inline_comments=2))

    assert len(plan.inline_comments) == 2
    assert len(plan.summary_only) == 3
    assert plan.omitted == ()


def test_max_summary_findings_cap_produces_omitted() -> None:
    # All unmappable (forces everything into the summary bucket), more
    # than the summary cap.
    findings = [_finding(path=f"src/missing_{i}.py") for i in range(5)]
    plan = _build(findings, config=PublicationConfig(min_severity=Severity.INFO, max_summary_findings=2))

    assert plan.inline_comments == ()
    assert len(plan.summary_only) == 2
    assert len(plan.omitted) == 3
    assert all("cap" in c.reason for c in plan.omitted)


def test_selection_prefers_higher_severity_under_inline_cap() -> None:
    low = _finding(line=2, severity=Severity.LOW, title="low")
    critical = _finding(line=3, severity=Severity.CRITICAL, title="critical")
    plan = _build([low, critical], config=PublicationConfig(min_severity=Severity.INFO, max_inline_comments=1))

    assert len(plan.inline_comments) == 1
    assert plan.inline_comments[0].severity is Severity.CRITICAL
    assert len(plan.summary_only) == 1
    assert plan.summary_only[0].severity is Severity.LOW


def test_duplicate_findings_same_location_both_get_distinct_fingerprints() -> None:
    a = _finding(line=5, title="dup-a")
    b = _finding(line=5, title="dup-b")
    plan = _build([a, b])
    fingerprints = {c.fingerprint for c in plan.inline_comments}
    # Different finding_id/title/message -> different fingerprints even
    # at the identical location.
    assert len(fingerprints) == 2


def test_presentation_ordering_is_path_then_line_then_severity_then_fingerprint() -> None:
    f1 = _finding(line=20, severity=Severity.LOW)
    f2 = _finding(line=5, severity=Severity.HIGH)
    f3 = _finding(line=5, severity=Severity.CRITICAL)
    plan = _build([f1, f2, f3], config=PublicationConfig(min_severity=Severity.INFO, max_inline_comments=10))

    lines = [c.position.line for c in plan.inline_comments if c.position is not None]
    assert lines == sorted(lines)  # ascending by line within the same path


def test_repeated_calls_with_identical_input_produce_identical_plans() -> None:
    findings = [_finding(line=i, severity=Severity.HIGH) for i in (5, 10, 15)]
    pub_id = uuid.uuid4()
    plan_a = _build(findings, publication_id=pub_id)
    plan_b = _build(findings, publication_id=pub_id)

    assert plan_a.inline_comments == plan_b.inline_comments
    assert plan_a.summary_body == plan_b.summary_body
    assert plan_a.status == plan_b.status


def test_disabled_mode_does_not_change_planning() -> None:
    """No separate behavior logic between DRY_RUN and PUBLISH inside the
    planner -- only patchfrog.publishing.service decides whether to
    actually write, based on `mode`."""

    findings = [_finding(line=5, severity=Severity.HIGH)]
    dry_run_plan = _build(findings, mode=ReviewPublicationMode.DRY_RUN)
    publish_plan = _build(findings, mode=ReviewPublicationMode.PUBLISH)

    assert len(dry_run_plan.inline_comments) == len(publish_plan.inline_comments) == 1
    assert dry_run_plan.inline_comments[0].fingerprint == publish_plan.inline_comments[0].fingerprint
