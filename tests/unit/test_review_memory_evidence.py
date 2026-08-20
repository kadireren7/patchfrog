"""Unit coverage for :mod:`patchfrog.review_memory.evidence` -- pure,
deterministic evidence-snippet revalidation. No I/O, no LLM, no fuzzy
matching in this module; ``current_file_contents`` here stands in for
an already-fetched exact-current-commit read
(:func:`patchfrog.repository.file_contents.read_files_at_commit`)."""

from __future__ import annotations

import uuid

from patchfrog.analysis.domain import FindingCategory, Severity
from patchfrog.domain.code import Language, SymbolKind
from patchfrog.review_memory.domain import (
    EvidenceSnippet,
    FindingMemoryStatus,
    ReviewMemoryFinding,
    SymbolContinuityResult,
    SymbolContinuityStatus,
    SymbolSnapshot,
)
from patchfrog.review_memory.evidence import (
    determine_evidence_check_targets,
    revalidate_evidence,
)

_REPO = uuid.uuid4()
_PR = uuid.uuid4()


def _finding(
    *, symbol_id: uuid.UUID | None, evidence: tuple[EvidenceSnippet, ...] = (),
    file_path: str = "a.py",
) -> ReviewMemoryFinding:
    return ReviewMemoryFinding(
        id=uuid.uuid4(), source_review_run_id=uuid.uuid4(), source_finding_id=uuid.uuid4(),
        repository_id=_REPO, pull_request_id=_PR, first_seen_commit_sha="sha1", last_seen_commit_sha="sha1",
        file_path=file_path, symbol_id=symbol_id, symbol_qualified_name="foo" if symbol_id else None,
        symbol_kind=SymbolKind.FUNCTION if symbol_id else None, category=FindingCategory.CORRECTNESS,
        severity=Severity.HIGH, title="t", message="m", start_line=1, end_line=2,
        exact_fingerprint="e", semantic_family_fingerprint="f", status=FindingMemoryStatus.OPEN,
        evidence=evidence,
    )


def _snapshot(*, id_: uuid.UUID, file_path: str = "a.py") -> SymbolSnapshot:
    return SymbolSnapshot(
        id=id_, name="foo", qualified_name="foo", kind=SymbolKind.FUNCTION, language=Language.PYTHON,
        file_path=file_path, start_line=1, end_line=5, content_hash="h",
    )


def _continuity(
    *, status: SymbolContinuityStatus, previous_id: uuid.UUID, current_id: uuid.UUID | None,
    current_file_path: str = "a.py",
) -> SymbolContinuityResult:
    current = _snapshot(id_=current_id, file_path=current_file_path) if current_id is not None else None
    return SymbolContinuityResult(
        status=status, previous=_snapshot(id_=previous_id), current=current, reason="r"
    )


def test_unchanged_symbol_with_matching_evidence_confirms_true() -> None:
    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(symbol_id=prev_id, evidence=(EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="return a - b"),))
    continuity = _continuity(status=SymbolContinuityStatus.UNCHANGED, previous_id=prev_id, current_id=cur_id)

    targets = determine_evidence_check_targets(previous_findings=[finding], symbol_changes=[continuity])
    assert targets == {finding.id: "a.py"}

    result = revalidate_evidence(
        previous_findings=[finding], symbol_changes=[continuity],
        current_file_contents={"a.py": "def foo():\n    return a - b\n"},
    )
    assert result[str(finding.id)] is True


def test_moved_symbol_body_identical_evidence_confirms_true() -> None:
    """Regression scenario B: function relocates lines, body/evidence
    identical -> deterministic line relocation succeeds, carried forward."""

    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(symbol_id=prev_id, evidence=(EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="return a - b"),))
    continuity = _continuity(status=SymbolContinuityStatus.MOVED, previous_id=prev_id, current_id=cur_id)

    result = revalidate_evidence(
        previous_findings=[finding], symbol_changes=[continuity],
        current_file_contents={"a.py": "# 40 lines of unrelated padding\n" * 40 + "def foo():\n    return a - b\n"},
    )
    assert result[str(finding.id)] is True


def test_renamed_file_exact_symbol_survives_confirms_true() -> None:
    """Regression scenario C: file renamed but exact symbol/evidence
    survives -> carry forward if deterministic lineage is proven."""

    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(symbol_id=prev_id, file_path="old_name.py", evidence=(EvidenceSnippet(file_path="old_name.py", start_line=2, end_line=2, quoted_text="return a - b"),))
    continuity = _continuity(
        status=SymbolContinuityStatus.RENAMED, previous_id=prev_id, current_id=cur_id, current_file_path="new_name.py"
    )

    targets = determine_evidence_check_targets(previous_findings=[finding], symbol_changes=[continuity])
    assert targets == {finding.id: "new_name.py"}  # mapped to the NEW path, not the stale one

    result = revalidate_evidence(
        previous_findings=[finding], symbol_changes=[continuity],
        current_file_contents={"new_name.py": "def foo():\n    return a - b\n"},
    )
    assert result[str(finding.id)] is True


def test_evidence_text_no_longer_present_fails_closed() -> None:
    """Regression scenario D: one evidence line changes inside an
    otherwise-same symbol (as far as this deterministic text check is
    concerned) -> needs recheck, never silently trusted."""

    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(symbol_id=prev_id, evidence=(EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="return a - b"),))
    continuity = _continuity(status=SymbolContinuityStatus.UNCHANGED, previous_id=prev_id, current_id=cur_id)

    result = revalidate_evidence(
        previous_findings=[finding], symbol_changes=[continuity],
        current_file_contents={"a.py": "def foo():\n    return a / b\n"},  # evidence text absent
    )
    assert result[str(finding.id)] is False


def test_missing_evidence_fails_closed() -> None:
    """A finding with zero stored evidence (e.g. a pre-evidence-revalidation
    row) can never be confirmed -- there is nothing deterministic to
    check against."""

    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(symbol_id=prev_id, evidence=())
    continuity = _continuity(status=SymbolContinuityStatus.UNCHANGED, previous_id=prev_id, current_id=cur_id)

    result = revalidate_evidence(
        previous_findings=[finding], symbol_changes=[continuity],
        current_file_contents={"a.py": "def foo():\n    return a - b\n"},
    )
    assert result[str(finding.id)] is False


def test_unreadable_file_fails_closed() -> None:
    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(symbol_id=prev_id, evidence=(EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="return a - b"),))
    continuity = _continuity(status=SymbolContinuityStatus.UNCHANGED, previous_id=prev_id, current_id=cur_id)

    result = revalidate_evidence(
        previous_findings=[finding], symbol_changes=[continuity],
        current_file_contents={"a.py": None},  # fetch failed / deleted / binary
    )
    assert result[str(finding.id)] is False


def test_all_evidence_snippets_must_match() -> None:
    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(
        symbol_id=prev_id,
        evidence=(
            EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="return a - b"),
            EvidenceSnippet(file_path="a.py", start_line=1, end_line=1, quoted_text="def foo(a, b):"),
            EvidenceSnippet(file_path="a.py", start_line=99, end_line=99, quoted_text="this line does not exist"),
        ),
    )
    continuity = _continuity(status=SymbolContinuityStatus.UNCHANGED, previous_id=prev_id, current_id=cur_id)

    result = revalidate_evidence(
        previous_findings=[finding], symbol_changes=[continuity],
        current_file_contents={"a.py": "def foo(a, b):\n    return a - b\n"},
    )
    assert result[str(finding.id)] is False  # the third snippet is missing -> whole finding fails closed


def test_whitespace_only_differences_still_match() -> None:
    """Normalization is whitespace-collapse only -- never fuzzy semantic
    matching."""

    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(symbol_id=prev_id, evidence=(EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="  return   a - b  "),))
    continuity = _continuity(status=SymbolContinuityStatus.UNCHANGED, previous_id=prev_id, current_id=cur_id)

    result = revalidate_evidence(
        previous_findings=[finding], symbol_changes=[continuity],
        current_file_contents={"a.py": "def foo():\n\treturn a - b\n"},
    )
    assert result[str(finding.id)] is True


def test_modified_symbol_is_never_a_check_target() -> None:
    """MODIFIED continuity never even reaches evidence revalidation --
    it always needs a fresh AI look regardless of what the evidence text
    says (the resolver never consults evidence_confirmed for MODIFIED)."""

    prev_id, cur_id = uuid.uuid4(), uuid.uuid4()
    finding = _finding(symbol_id=prev_id, evidence=(EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="return a - b"),))
    continuity = _continuity(status=SymbolContinuityStatus.MODIFIED, previous_id=prev_id, current_id=cur_id)

    targets = determine_evidence_check_targets(previous_findings=[finding], symbol_changes=[continuity])
    assert targets == {}


def test_ambiguous_symbol_is_never_a_check_target() -> None:
    """Regression scenario F: ambiguous symbol continuity -> never
    eligible for evidence-based carry-forward, always re-review."""

    prev_id = uuid.uuid4()
    finding = _finding(symbol_id=prev_id, evidence=(EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="return a - b"),))
    continuity = SymbolContinuityResult(
        status=SymbolContinuityStatus.AMBIGUOUS, previous=_snapshot(id_=prev_id), current=None, reason="ambiguous"
    )

    targets = determine_evidence_check_targets(previous_findings=[finding], symbol_changes=[continuity])
    assert targets == {}


def test_module_level_finding_is_never_a_check_target() -> None:
    finding = _finding(symbol_id=None, evidence=(EvidenceSnippet(file_path="a.py", start_line=2, end_line=2, quoted_text="x"),))
    targets = determine_evidence_check_targets(previous_findings=[finding], symbol_changes=[])
    assert targets == {}


def test_no_targets_means_no_fetch_needed() -> None:
    """A pure sanity check that a fully-empty scenario doesn't crash and
    correctly reports nothing needs fetching -- the caller
    (IncrementalReviewMemoryService._revalidate_evidence) uses this to
    skip the git read entirely."""

    assert determine_evidence_check_targets(previous_findings=[], symbol_changes=[]) == {}
    assert revalidate_evidence(previous_findings=[], symbol_changes=[], current_file_contents={}) == {}
