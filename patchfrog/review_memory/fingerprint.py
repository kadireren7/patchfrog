"""Finding fingerprints for review memory.

Two deliberately different fingerprints (per the Phase 7 spec):

``exact_fingerprint`` -- file, symbol/range, category, normalized
message family, scoped to one specific commit. Repeat detection: "is
this literally the same finding I already have at this exact commit."

``semantic_family_fingerprint`` -- symbol *identity* (qualified name,
not line numbers), category, normalized title/message family. Stable
across line movement and file renames. This is also used directly as
the cross-commit finding identity for publication suppression (see
:mod:`patchfrog.publishing.planner`'s ``already_reported_finding_ids``)
-- a deliberate, documented simplification rather than a third parallel
concept: both uses need exactly "the same underlying issue, independent
of which commit/line it's currently at", so one fingerprint serves both.
It is intentionally a *different* value from Phase 6's own per-publication
fingerprint (:func:`patchfrog.publishing.fingerprint.compute_finding_fingerprint`,
which is deliberately commit-scoped) -- Phase 6's exact-SHA idempotency
is never weakened by this; the two fingerprints answer different
questions and are never substituted for each other.

Neither fingerprint uses an LLM embedding or fuzzy text similarity --
see the module docstring of :mod:`patchfrog.review_memory`.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from patchfrog.analysis.domain import FindingCategory


def _normalize_message(text: str) -> str:
    return " ".join(text.split()).strip().lower()


def compute_exact_fingerprint(
    *,
    repository_id: UUID,
    pull_request_id: UUID,
    commit_sha: str,
    file_path: str,
    start_line: int,
    end_line: int,
    category: FindingCategory,
    message: str,
) -> str:
    parts = (
        str(repository_id), str(pull_request_id), commit_sha, file_path,
        str(start_line), str(end_line), category.value, _normalize_message(message),
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def compute_semantic_family_fingerprint(
    *,
    repository_id: UUID,
    pull_request_id: UUID,
    file_path: str,
    symbol_qualified_name: str | None,
    category: FindingCategory,
    title: str,
) -> str:
    """Deliberately excludes ``commit_sha`` and line numbers -- this is
    what makes it stable across line movement, and what makes it usable
    as a cross-commit finding identity (see the module docstring)."""

    symbol_key = symbol_qualified_name or f"__module__:{file_path}"
    parts = (str(repository_id), str(pull_request_id), file_path, symbol_key, category.value, _normalize_message(title))
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
