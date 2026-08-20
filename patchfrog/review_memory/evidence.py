"""Deterministic evidence-snippet revalidation for zero-AI-call
carry-forward.

No LLM, no fuzzy matching, no I/O in this module -- both functions here
take plain values (including already-fetched real current-commit file
content) and return plain values, so both are fully unit-testable. The
actual fetch of that current-commit content
(:func:`patchfrog.repository.file_contents.read_files_at_commit`) always
targets the *exact* current commit via a fresh, targeted git read --
never a previously-built :class:`~patchfrog.context.domain.ContextBundle`,
which is built for LLM prompt construction and can be stale, truncated,
or scoped differently than what a byte-exact evidence check needs.

Only ``UNCHANGED``/``MOVED``/``RENAMED`` symbol continuity even reaches
this check (see :meth:`patchfrog.review_memory.resolver.ReviewMemoryResolver._resolve_from_continuity`)
-- ``MODIFIED``/``AMBIGUOUS``/no-continuity findings always need a fresh
AI look regardless of what this module would say, so this module is
never even asked about them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from patchfrog.review_memory.domain import (
    ReviewMemoryFinding,
    SymbolContinuityResult,
    SymbolContinuityStatus,
)

_CARRY_FORWARD_STATUSES = (
    SymbolContinuityStatus.UNCHANGED,
    SymbolContinuityStatus.MOVED,
    SymbolContinuityStatus.RENAMED,
)


def determine_evidence_check_targets(
    *,
    previous_findings: Sequence[ReviewMemoryFinding],
    symbol_changes: Sequence[SymbolContinuityResult],
) -> dict[UUID, str]:
    """The current-commit file path to read for every finding whose
    symbol continuity is even eligible for evidence-based carry-forward
    -- i.e. exactly the findings :func:`revalidate_evidence` can produce
    a ``True`` for. Callers use this to fetch only the files actually
    needed, never the whole repository."""

    by_previous_symbol_id = {r.previous.id: r for r in symbol_changes if r.previous is not None}
    targets: dict[UUID, str] = {}
    for finding in previous_findings:
        if finding.symbol_id is None:
            continue
        continuity = by_previous_symbol_id.get(finding.symbol_id)
        if continuity is None or continuity.status not in _CARRY_FORWARD_STATUSES:
            continue
        if continuity.current is None:
            continue
        targets[finding.id] = continuity.current.file_path
    return targets


def revalidate_evidence(
    *,
    previous_findings: Sequence[ReviewMemoryFinding],
    symbol_changes: Sequence[SymbolContinuityResult],
    current_file_contents: Mapping[str, str | None],
) -> dict[str, bool]:
    """For every finding returned by :func:`determine_evidence_check_targets`,
    deterministically confirm whether *every* stored evidence snippet is
    still present verbatim (whitespace-normalized only -- no fuzzy
    matching) in the mapped file's exact current-commit content.

    Fails closed (``False``, meaning "needs recheck") whenever:
      - the finding has zero stored evidence (nothing to confirm against,
        e.g. a pre-evidence-revalidation row), or
      - the mapped file's content couldn't be read (``current_file_contents``
        maps it to ``None`` -- deleted, unreadable, binary, fetch failure).

    Returns a ``{str(finding.id): bool}`` mapping in the exact shape
    :meth:`patchfrog.review_memory.resolver.ReviewMemoryResolver.resolve_pre_review`
    already accepts as ``evidence_still_present`` -- entries for findings
    outside :func:`determine_evidence_check_targets` are simply omitted,
    which the resolver already treats as "not confirmed" by default.
    """

    targets = determine_evidence_check_targets(
        previous_findings=previous_findings, symbol_changes=symbol_changes
    )
    findings_by_id = {f.id: f for f in previous_findings}
    result: dict[str, bool] = {}
    for finding_id, file_path in targets.items():
        finding = findings_by_id[finding_id]
        content = current_file_contents.get(file_path)
        if content is None or not finding.evidence:
            result[str(finding_id)] = False
            continue
        normalized_content = _normalize(content)
        result[str(finding_id)] = all(
            _normalize(snippet.quoted_text) in normalized_content for snippet in finding.evidence
        )
    return result


def _normalize(text: str) -> str:
    return " ".join(text.split())
